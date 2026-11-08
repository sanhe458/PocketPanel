package main

import (
	"crypto/rand"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ================= 分享链接 =================
//
// 只读、可撤销、默认 24 小时、默认带 4 位提取码。
// 只允许分享文件：目录分享会诱导用户暴露整棵目录树，且需要打包任务，
// 这里直接要求先压缩，语义更清楚。

type Share struct {
	Token     string `json:"token"`
	Path      string `json:"path"`
	Name      string `json:"name"`
	Size      int64  `json:"size"`
	CreatedAt int64  `json:"created_at"`
	ExpiresAt int64  `json:"expires_at"`
	Code      string `json:"code"`
	Downloads int    `json:"downloads"`
}

var (
	shareMu    sync.Mutex
	shareFails = map[string]*shareFail{}
)

type shareFail struct {
	Count int
	Until time.Time
}

func shareFile() string { return filepath.Join(dataDir, "shares.json") }

func loadShares() []Share {
	b, err := os.ReadFile(shareFile())
	if err != nil {
		return []Share{}
	}
	var out []Share
	json.Unmarshal(b, &out)
	return out
}

func saveShares(list []Share) {
	b, _ := json.MarshalIndent(list, "", " ")
	os.WriteFile(shareFile(), b, 0600)
}

func randCode(n int) string {
	const cs = "23456789abcdefghjkmnpqrstuvwxyz"
	raw := make([]byte, n)
	rand.Read(raw)
	for i := range raw {
		raw[i] = cs[int(raw[i])%len(cs)]
	}
	return string(raw)
}

func handleShareCreate(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Path      string `json:"path"`
		Hours     int    `json:"hours"`
		NeedCode  bool   `json:"need_code"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	abs, err := sandboxPath(body.Path)
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	st, err := os.Stat(abs)
	if err != nil {
		apiErr(w, 404, "文件不存在")
		return
	}
	if st.IsDir() {
		apiErr(w, 400, "暂不支持分享目录，请先压缩为 zip 再分享")
		return
	}
	if body.Hours <= 0 {
		body.Hours = 24
	}
	if body.Hours > 24*30 {
		body.Hours = 24 * 30
	}
	s := Share{
		Token:     randHex(12),
		Path:      abs,
		Name:      filepath.Base(abs),
		Size:      st.Size(),
		CreatedAt: time.Now().Unix(),
		ExpiresAt: time.Now().Add(time.Duration(body.Hours) * time.Hour).Unix(),
	}
	if body.NeedCode {
		s.Code = randCode(4)
	}
	shareMu.Lock()
	list := loadShares()
	list = append(list, s)
	saveShares(list)
	shareMu.Unlock()
	writeJSON(w, 200, s)
}

func handleShareList(w http.ResponseWriter, r *http.Request) {
	shareMu.Lock()
	list := loadShares()
	shareMu.Unlock()
	now := time.Now().Unix()
	out := make([]map[string]interface{}, 0, len(list))
	for _, s := range list {
		out = append(out, map[string]interface{}{
			"token": s.Token, "name": s.Name, "path": s.Path, "size": s.Size,
			"created_at": s.CreatedAt, "expires_at": s.ExpiresAt,
			"code": s.Code, "downloads": s.Downloads, "expired": s.ExpiresAt < now,
		})
	}
	writeJSON(w, 200, out)
}

func handleShareRevoke(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Token string `json:"token"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	shareMu.Lock()
	list := loadShares()
	kept := list[:0]
	for _, s := range list {
		if s.Token != body.Token {
			kept = append(kept, s)
		}
	}
	saveShares(kept)
	shareMu.Unlock()
	writeJSON(w, 200, map[string]string{"ok": "1"})
}

func findShare(token string) (Share, int) {
	list := loadShares()
	for i, s := range list {
		if s.Token == token {
			return s, i
		}
	}
	return Share{}, -1
}

// 公开下载：不需要登录，但必须校验有效期与提取码
func handlePublicShare(w http.ResponseWriter, r *http.Request) {
	rest := strings.TrimPrefix(r.URL.Path, "/api/public/share/")
	parts := strings.Split(rest, "/")
	if len(parts) != 2 || parts[1] != "download" {
		apiErr(w, 404, "not found")
		return
	}
	token := parts[0]
	shareMu.Lock()
	s, _ := findShare(token)
	shareMu.Unlock()
	if s.Token == "" {
		apiErr(w, 404, "分享不存在或已撤销")
		return
	}
	if time.Now().Unix() > s.ExpiresAt {
		apiErr(w, 410, "分享已过期")
		return
	}
	if s.Code != "" {
		shareMu.Lock()
		f := shareFails[token]
		if f != nil && f.Count >= 10 && time.Now().Before(f.Until) {
			shareMu.Unlock()
			apiErr(w, 429, "提取码错误次数过多，请 10 分钟后再试")
			return
		}
		shareMu.Unlock()
		if r.URL.Query().Get("code") != s.Code {
			shareMu.Lock()
			if shareFails[token] == nil {
				shareFails[token] = &shareFail{}
			}
			shareFails[token].Count++
			shareFails[token].Until = time.Now().Add(10 * time.Minute)
			shareMu.Unlock()
			apiErr(w, 403, "提取码错误")
			return
		}
		shareMu.Lock()
		delete(shareFails, token)
		shareMu.Unlock()
	}
	f, err := os.Open(s.Path)
	if err != nil {
		apiErr(w, 404, "文件已被移动或删除")
		return
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		apiErr(w, 500, err.Error())
		return
	}
	shareMu.Lock()
	list := loadShares()
	if idx, err := findShareIdx(list, token); err == nil {
		list[idx].Downloads++
		saveShares(list)
	}
	shareMu.Unlock()

	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Disposition", "attachment; filename*=UTF-8''"+urlEncode(s.Name))
	http.ServeContent(w, r, s.Name, st.ModTime(), f)
}

func findShareIdx(list []Share, token string) (int, error) {
	for i, s := range list {
		if s.Token == token {
			return i, nil
		}
	}
	return -1, fmt.Errorf("not found")
}

func urlEncode(s string) string {
	var b strings.Builder
	for _, c := range []byte(s) {
		if (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '.' || c == '-' || c == '_' {
			b.WriteByte(c)
		} else {
			b.WriteString(fmt.Sprintf("%%%02X", c))
		}
	}
	return b.String()
}

const sharePageTpl = `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;
 background:#080b11;color:#e7eef9;font-family:-apple-system,"PingFang SC",sans-serif}
.card{width:100%%;max-width:380px;background:#101722;border:1px solid #1e2a3a;border-radius:16px;padding:24px}
.logo{width:40px;height:40px;border-radius:11px;background:linear-gradient(140deg,#4d8dff,#22d3ee);
 display:grid;place-items:center;color:#05121f;font-weight:800;margin-bottom:14px}
h1{font-size:16px;margin:0 0 6px;word-break:break-all}
p{margin:0 0 18px;font-size:12px;color:#5f7089}
input{width:100%%;height:40px;border-radius:9px;border:1px solid #1e2a3a;background:#0c1118;color:#e7eef9;
 padding:0 12px;outline:none;font-size:15px;letter-spacing:4px;text-align:center;text-transform:lowercase}
input:focus{border-color:#4d8dff}
button{width:100%%;height:40px;margin-top:12px;border:0;border-radius:9px;background:#4d8dff;color:#fff;
 font-weight:600;font-size:14px}
button:disabled{opacity:.5}
.err{color:#f05252;font-size:12px;min-height:16px;margin-top:8px}
.meta{font-size:11.5px;color:#5f7089;margin-top:14px;line-height:1.7}
</style></head><body>
<div class="card">
  <div class="logo">P</div>
  <h1>%s</h1>
  <p>PocketPanel 分享 · %s</p>
  %s
  <div class="err" id="err"></div>
  <div class="meta">有效期至 %s<br>只读分享，链接可被分享者随时撤销。</div>
</div>
<script>
document.getElementById('go').onclick=function(){
  var code=document.getElementById('code').value.trim();
  if(!code){document.getElementById('err').textContent='请输入提取码';return}
  location.href='/api/public/share/%s/download?code='+encodeURIComponent(code);
};
</script>
</body></html>`

func handleSharePage(w http.ResponseWriter, r *http.Request) {
	token := strings.TrimPrefix(r.URL.Path, "/s/")
	s, _ := findShare(token)
	if s.Token == "" || time.Now().Unix() > s.ExpiresAt {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(404)
		fmt.Fprintf(w, sharePageTpl, "分享已失效", "分享已失效",
			"链接不存在或已过期", "<p>请联系分享者重新生成。</p>", "—", token)
		return
	}
	codeBox := ""
	if s.Code != "" {
		codeBox = `<input id="code" maxlength="4" placeholder="提取码" autocomplete="off">
<button id="go">下载</button>`
	} else {
		codeBox = `<button id="go" onclick="location.href='/api/public/share/` + token + `/download'">下载文件</button>`
	}
	size := humanSize(s.Size)
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprintf(w, sharePageTpl, s.Name, s.Name, size, codeBox,
		time.Unix(s.ExpiresAt, 0).Format("2006-01-02 15:04"), token)
}

// ================= 分片上传 =================
//
// 客户端按 1MB 切片，服务端每片独立落盘，全部到齐后按序合并。
// 断点续传依赖 GET /api/upload/<id> 返回已收到的分片索引，
// 客户端重新选择同一文件时先查这个接口，跳过已传分片。

const (
	uploadMaxChunk = 8 << 20
	uploadMaxParts = 20000
)

type UploadMeta struct {
	ID        string `json:"id"`
	Path      string `json:"path"` // 目标绝对路径
	Name      string `json:"name"`
	Size      int64  `json:"size"`
	ChunkSize int64  `json:"chunk_size"`
	Total     int    `json:"total"`
	CreatedAt int64  `json:"created_at"`
}

func uploadsDir() string { return filepath.Join(dataDir, "uploads") }

func loadUploadMeta(id string) (UploadMeta, error) {
	var m UploadMeta
	if validName(id) != nil {
		return m, fmt.Errorf("上传 ID 非法")
	}
	b, err := os.ReadFile(filepath.Join(uploadsDir(), id, "meta.json"))
	if err != nil {
		return m, fmt.Errorf("上传任务不存在或已过期")
	}
	if err := json.Unmarshal(b, &m); err != nil {
		return m, fmt.Errorf("上传元数据损坏")
	}
	return m, nil
}

func handleUploadInit(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Path      string `json:"path"`
		Name      string `json:"name"`
		Size      int64  `json:"size"`
		ChunkSize int64  `json:"chunk_size"`
		ID        string `json:"id"` // 客户端可带上已有 ID 继续
	}
	json.NewDecoder(r.Body).Decode(&body)
	if err := validName(body.Name); err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	dir, err := sandboxPath(body.Path)
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	if st, err := os.Stat(dir); err != nil || !st.IsDir() {
		apiErr(w, 400, "目标不是目录")
		return
	}
	if body.Size <= 0 {
		apiErr(w, 400, "文件大小无效")
		return
	}
	cs := body.ChunkSize
	if cs <= 0 || cs > uploadMaxChunk {
		cs = 1 << 20
	}
	total := int((body.Size + cs - 1) / cs)
	if total > uploadMaxParts {
		apiErr(w, 400, "分片数量过多，请增大分片大小")
		return
	}
	id := body.ID
	if id == "" || validName(id) != nil {
		id = randHex(10)
	}
	m := UploadMeta{
		ID: id, Path: filepath.Join(dir, body.Name), Name: body.Name,
		Size: body.Size, ChunkSize: cs, Total: total, CreatedAt: time.Now().Unix(),
	}
	ud := filepath.Join(uploadsDir(), id)
	if err := os.MkdirAll(ud, 0700); err != nil {
		apiErr(w, 500, err.Error())
		return
	}
	b, _ := json.Marshal(m)
	os.WriteFile(filepath.Join(ud, "meta.json"), b, 0600)
	writeJSON(w, 200, map[string]interface{}{
		"id": id, "total": total, "chunk_size": cs, "uploaded": uploadedParts(id),
	})
}

func uploadedParts(id string) []int {
	out := []int{}
	ents, err := os.ReadDir(filepath.Join(uploadsDir(), id))
	if err != nil {
		return out
	}
	for _, e := range ents {
		n := strings.TrimSuffix(e.Name(), ".part")
		if n == e.Name() {
			continue
		}
		if v, err := strconv.Atoi(n); err == nil {
			out = append(out, v)
		}
	}
	return out
}

func handleUploadStatus(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/api/upload/")
	m, err := loadUploadMeta(id)
	if err != nil {
		apiErr(w, 404, err.Error())
		return
	}
	writeJSON(w, 200, map[string]interface{}{
		"id": m.ID, "name": m.Name, "size": m.Size, "total": m.Total,
		"chunk_size": m.ChunkSize, "uploaded": uploadedParts(m.ID),
	})
}

func handleUploadChunk(w http.ResponseWriter, r *http.Request) {
	rest := strings.TrimPrefix(r.URL.Path, "/api/upload/")
	parts := strings.Split(rest, "/")
	if len(parts) != 2 {
		apiErr(w, 400, "bad path")
		return
	}
	id := parts[0]
	idx, err := strconv.Atoi(parts[1])
	if err != nil || idx < 0 {
		apiErr(w, 400, "分片序号非法")
		return
	}
	m, err := loadUploadMeta(id)
	if err != nil {
		apiErr(w, 404, err.Error())
		return
	}
	if idx >= m.Total {
		apiErr(w, 400, "分片序号超出范围")
		return
	}
	if r.ContentLength > uploadMaxChunk {
		apiErr(w, 413, "分片超过 8MB")
		return
	}
	f, err := os.Create(filepath.Join(uploadsDir(), id, fmt.Sprintf("%d.part", idx)))
	if err != nil {
		apiErr(w, 500, err.Error())
		return
	}
	n, err := io.Copy(f, io.LimitReader(r.Body, uploadMaxChunk+1))
	f.Close()
	if err != nil {
		apiErr(w, 500, err.Error())
		return
	}
	if n > uploadMaxChunk {
		os.Remove(filepath.Join(uploadsDir(), id, fmt.Sprintf("%d.part", idx)))
		apiErr(w, 413, "分片超过 8MB")
		return
	}
	writeJSON(w, 200, map[string]interface{}{"ok": "1", "index": idx, "size": n})
}

func handleUploadComplete(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/api/upload/")
	id = strings.TrimSuffix(id, "/complete")
	m, err := loadUploadMeta(id)
	if err != nil {
		apiErr(w, 404, err.Error())
		return
	}
	ud := filepath.Join(uploadsDir(), id)
	// 校验分片齐全且大小正确
	var sum int64
	for i := 0; i < m.Total; i++ {
		p := filepath.Join(ud, fmt.Sprintf("%d.part", i))
		st, err := os.Stat(p)
		if err != nil {
			apiErr(w, 400, fmt.Sprintf("分片 %d 缺失，请继续上传", i))
			return
		}
		sum += st.Size()
	}
	if sum != m.Size {
		apiErr(w, 400, fmt.Sprintf("分片总大小 %d 与声明 %d 不一致", sum, m.Size))
		return
	}
	target := uniquePath(m.Path)
	tmp := target + ".uploading"
	out, err := os.Create(tmp)
	if err != nil {
		apiErr(w, 500, err.Error())
		return
	}
	for i := 0; i < m.Total; i++ {
		in, err := os.Open(filepath.Join(ud, fmt.Sprintf("%d.part", i)))
		if err != nil {
			out.Close()
			os.Remove(tmp)
			apiErr(w, 500, err.Error())
			return
		}
		_, err = io.Copy(out, in)
		in.Close()
		if err != nil {
			out.Close()
			os.Remove(tmp)
			apiErr(w, 500, err.Error())
			return
		}
	}
	out.Close()
	if err := os.Rename(tmp, target); err != nil {
		apiErr(w, 500, err.Error())
		return
	}
	os.RemoveAll(ud)
	writeJSON(w, 200, map[string]interface{}{"ok": "1", "path": target, "size": m.Size})
}

func handleUploadAbort(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/api/upload/")
	id = strings.TrimSuffix(id, "/abort")
	if validName(id) != nil {
		apiErr(w, 400, "上传 ID 非法")
		return
	}
	os.RemoveAll(filepath.Join(uploadsDir(), id))
	writeJSON(w, 200, map[string]string{"ok": "1"})
}

// cleanUploads 清理超过 24 小时未完成的分片目录
func cleanUploads() {
	ents, err := os.ReadDir(uploadsDir())
	if err != nil {
		return
	}
	deadline := time.Now().Add(-24 * time.Hour)
	for _, e := range ents {
		info, err := e.Info()
		if err != nil {
			continue
		}
		if info.ModTime().Before(deadline) {
			os.RemoveAll(filepath.Join(uploadsDir(), e.Name()))
		}
	}
}

// handleUploadRouter 按方法与后缀分发上传相关请求
func handleUploadRouter(w http.ResponseWriter, r *http.Request) {
	p := strings.TrimPrefix(r.URL.Path, "/api/upload/")
	switch {
	case strings.HasSuffix(p, "/complete") && r.Method == http.MethodPost:
		handleUploadComplete(w, r)
	case strings.HasSuffix(p, "/abort") && r.Method == http.MethodPost:
		handleUploadAbort(w, r)
	case r.Method == http.MethodPut:
		handleUploadChunk(w, r)
	default:
		handleUploadStatus(w, r)
	}
}
