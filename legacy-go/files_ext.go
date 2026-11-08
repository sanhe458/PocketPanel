package main

import (
	"bufio"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ================= 回收站 =================
//
// 删除不再是 rm -rf，而是把条目 rename 到 data/trash/items/<id>，并记录原路径。
// rename 在同一分区内是原子的，几十 GB 的目录也能瞬间"删除"。
// 索引落在 data/trash/index.json，条目数量级是几十到几百，全量读写足够。

type TrashItem struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	OrigPath  string `json:"orig_path"`
	Size      int64  `json:"size"`
	IsDir     bool   `json:"is_dir"`
	DeletedAt int64  `json:"deleted_at"`
}

var (
	trashMu sync.Mutex
)

func trashDir() string     { return filepath.Join(dataDir, "trash") }
func trashItems() string   { return filepath.Join(trashDir(), "items") }
func trashIndex() string   { return filepath.Join(trashDir(), "index.json") }

func loadTrash() []TrashItem {
	b, err := os.ReadFile(trashIndex())
	if err != nil {
		return []TrashItem{}
	}
	var out []TrashItem
	if json.Unmarshal(b, &out) != nil {
		return []TrashItem{}
	}
	return out
}

func saveTrash(items []TrashItem) error {
	os.MkdirAll(trashItems(), 0700)
	b, _ := json.MarshalIndent(items, "", " ")
	return os.WriteFile(trashIndex(), b, 0600)
}

// dirSize 统计目录大小，限制遍历节点数，避免超大目录把请求拖住
func dirSize(p string) int64 {
	st, err := os.Stat(p)
	if err != nil {
		return 0
	}
	if !st.IsDir() {
		return st.Size()
	}
	var total int64
	n := 0
	filepath.Walk(p, func(_ string, info os.FileInfo, err error) error {
		if err != nil {
			return nil
		}
		n++
		if n > 50000 {
			return fmt.Errorf("stop")
		}
		if !info.IsDir() {
			total += info.Size()
		}
		return nil
	})
	return total
}

func handleTrashList(w http.ResponseWriter, r *http.Request) {
	items := loadTrash()
	sort.Slice(items, func(i, j int) bool { return items[i].DeletedAt > items[j].DeletedAt })
	var total int64
	for _, it := range items {
		total += it.Size
	}
	writeJSON(w, 200, map[string]interface{}{
		"items": items, "count": len(items), "total_size": total,
		"keep_days": trashKeepDays(),
	})
}

func handleTrashDelete(w http.ResponseWriter, r *http.Request) {
	abs, err := sandboxPath(r.URL.Query().Get("path"))
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	if abs == homeDir() {
		apiErr(w, 400, "不能删除主目录")
		return
	}
	// 面板自身的数据目录不参与回收站，否则删掉后无法恢复
	if abs == dataDir || strings.HasPrefix(abs, dataDir+"/") {
		apiErr(w, 400, "面板数据目录不可删除")
		return
	}
	st, err := os.Stat(abs)
	if err != nil {
		apiErr(w, 400, "文件不存在")
		return
	}
	id, err := moveToTrash(abs, st)
	if err != nil {
		apiErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]string{"ok": "1", "id": id})
}

// moveToTrash 把文件或目录移入回收站，返回条目 ID
func moveToTrash(abs string, st os.FileInfo) (string, error) {
	trashMu.Lock()
	defer trashMu.Unlock()

	id := fmt.Sprintf("%d%s", time.Now().UnixNano(), randHex(3))
	os.MkdirAll(trashItems(), 0700)
	dst := filepath.Join(trashItems(), id)
	if err := os.Rename(abs, dst); err != nil {
		// 跨分区时回退：复制后删除
		if err2 := copyTree(abs, dst); err2 != nil {
			return "", fmt.Errorf("移入回收站失败: %v", err2)
		}
		os.RemoveAll(abs)
	}
	items := loadTrash()
	items = append(items, TrashItem{
		ID: id, Name: filepath.Base(abs), OrigPath: abs,
		Size: dirSize(dst), IsDir: st.IsDir(), DeletedAt: time.Now().Unix(),
	})
	if err := saveTrash(items); err != nil {
		return id, err
	}
	return id, nil
}

func handleTrashRestore(w http.ResponseWriter, r *http.Request) {
	var body struct {
		ID  string   `json:"id"`
		IDs []string `json:"ids"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	ids := body.IDs
	if body.ID != "" {
		ids = append(ids, body.ID)
	}
	if len(ids) == 0 {
		apiErr(w, 400, "未指定条目")
		return
	}
	trashMu.Lock()
	defer trashMu.Unlock()

	items := loadTrash()
	restored, failed := 0, []string{}
	for _, id := range ids {
		idx := -1
		for i, it := range items {
			if it.ID == id {
				idx = i
				break
			}
		}
		if idx < 0 {
			failed = append(failed, id+": 条目不存在")
			continue
		}
		it := items[idx]
		if _, err := os.Stat(it.OrigPath); err == nil {
			failed = append(failed, it.Name+": 原位置已存在同名文件")
			continue
		}
		os.MkdirAll(filepath.Dir(it.OrigPath), 0755)
		if err := os.Rename(filepath.Join(trashItems(), id), it.OrigPath); err != nil {
			if err2 := copyTree(filepath.Join(trashItems(), id), it.OrigPath); err2 != nil {
				failed = append(failed, it.Name+": "+err2.Error())
				continue
			}
			os.RemoveAll(filepath.Join(trashItems(), id))
		}
		items = append(items[:idx], items[idx+1:]...)
		restored++
	}
	saveTrash(items)
	writeJSON(w, 200, map[string]interface{}{"restored": restored, "failed": failed})
}

func handleTrashPurge(w http.ResponseWriter, r *http.Request) {
	var body struct {
		ID  string   `json:"id"`
		IDs []string `json:"ids"`
		All bool     `json:"all"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	trashMu.Lock()
	defer trashMu.Unlock()

	items := loadTrash()
	if body.All {
		os.RemoveAll(trashItems())
		os.MkdirAll(trashItems(), 0700)
		saveTrash([]TrashItem{})
		writeJSON(w, 200, map[string]interface{}{"ok": "1", "purged": len(items)})
		return
	}
	ids := body.IDs
	if body.ID != "" {
		ids = append(ids, body.ID)
	}
	gone := map[string]bool{}
	for _, id := range ids {
		gone[id] = true
		os.RemoveAll(filepath.Join(trashItems(), id))
	}
	kept := items[:0]
	for _, it := range items {
		if !gone[it.ID] {
			kept = append(kept, it)
		}
	}
	saveTrash(kept)
	writeJSON(w, 200, map[string]interface{}{"ok": "1", "purged": len(ids)})
}

// autoCleanTrash 删除超过保留天数的条目，启动时与每小时各跑一次
func autoCleanTrash() {
	days := trashKeepDays()
	if days <= 0 {
		return
	}
	deadline := time.Now().Add(-time.Duration(days) * 24 * time.Hour).Unix()
	trashMu.Lock()
	defer trashMu.Unlock()
	items := loadTrash()
	kept := make([]TrashItem, 0, len(items))
	removed := 0
	for _, it := range items {
		if it.DeletedAt < deadline {
			os.RemoveAll(filepath.Join(trashItems(), it.ID))
			removed++
			continue
		}
		kept = append(kept, it)
	}
	if removed > 0 {
		saveTrash(kept)
	}
}

// ================= 历史版本 =================
//
// 每次保存前把旧内容复制到 data/versions/<sha1(path)>/<unix>.bak。
// 只对 8MB 以下的普通文件做快照，超过的不快照并在界面明示，避免把磁盘写满。

const (
	versionMaxSize  = 8 << 20
	versionKeepDef  = 10
)

type VersionItem struct {
	ID   string `json:"id"`
	Path string `json:"path"`
	Ts   int64  `json:"ts"`   // 秒，用于显示
	Nano int64  `json:"nano"` // 纳秒，用于排序：同一秒内的多次保存也能区分
	Size int64  `json:"size"`
}

var versionMu sync.Mutex

func versionsDir() string { return filepath.Join(dataDir, "versions") }
func versionIndex() string { return filepath.Join(versionsDir(), "index.json") }

func loadVersions() []VersionItem {
	b, err := os.ReadFile(versionIndex())
	if err != nil {
		return []VersionItem{}
	}
	var out []VersionItem
	if json.Unmarshal(b, &out) != nil {
		return []VersionItem{}
	}
	return out
}

func saveVersions(v []VersionItem) {
	os.MkdirAll(versionsDir(), 0700)
	b, _ := json.MarshalIndent(v, "", " ")
	os.WriteFile(versionIndex(), b, 0600)
}

func pathKey(p string) string {
	h := sha1.Sum([]byte(p))
	return hex.EncodeToString(h[:])[:16]
}

func versionFile(id string) string {
	// id = <pathKey>/<纳秒时间戳>
	parts := strings.SplitN(id, "/", 2)
	if len(parts) != 2 || validName(parts[0]) != nil || validName(parts[1]) != nil {
		return ""
	}
	for _, c := range parts[1] {
		if c < '0' || c > '9' {
			return ""
		}
	}
	return filepath.Join(versionsDir(), parts[0], parts[1]+".bak")
}

// snapshotVersion 在写入前保存旧内容，返回快照 ID（空表示未快照）
func snapshotVersion(abs string) string {
	st, err := os.Stat(abs)
	if err != nil || st.IsDir() || st.Size() == 0 || st.Size() > versionMaxSize {
		return ""
	}
	versionMu.Lock()
	defer versionMu.Unlock()

	key := pathKey(abs)
	dir := filepath.Join(versionsDir(), key)
	os.MkdirAll(dir, 0700)
	now := time.Now()
	ts, nano := now.Unix(), now.UnixNano()
	// 用纳秒命名：同一秒内连续保存两次也不会撞名或排错序
	id := fmt.Sprintf("%s/%d", key, nano)
	dst := filepath.Join(dir, strconv.FormatInt(nano, 10)+".bak")
	if err := copyFile(abs, dst); err != nil {
		return ""
	}

	items := loadVersions()
	items = append(items, VersionItem{ID: id, Path: abs, Ts: ts, Nano: nano, Size: st.Size()})
	// 按路径分组，只保留最近 N 份
	keep := versionKeep()
	count := 0
	kept := make([]VersionItem, 0, len(items))
	for i := len(items) - 1; i >= 0; i-- {
		if items[i].Path == abs {
			count++
			if count > keep {
				if f := versionFile(items[i].ID); f != "" {
					os.Remove(f)
				}
				continue
			}
		}
		kept = append(kept, items[i])
	}
	// 反转回时间正序
	for i, j := 0, len(kept)-1; i < j; i, j = i+1, j-1 {
		kept[i], kept[j] = kept[j], kept[i]
	}
	saveVersions(kept)
	return id
}

func handleVersionList(w http.ResponseWriter, r *http.Request) {
	abs, err := sandboxPath(r.URL.Query().Get("path"))
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	var out []VersionItem
	for _, v := range loadVersions() {
		if v.Path == abs {
			out = append(out, v)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Nano != out[j].Nano {
			return out[i].Nano > out[j].Nano
		}
		return out[i].Ts > out[j].Ts
	})
	writeJSON(w, 200, map[string]interface{}{"path": abs, "versions": out, "keep": versionKeep()})
}

func handleVersionRead(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("id")
	f := versionFile(id)
	if f == "" {
		apiErr(w, 400, "版本 ID 非法")
		return
	}
	b, err := os.ReadFile(f)
	if err != nil {
		apiErr(w, 404, "版本不存在")
		return
	}
	if len(b) > 512*1024 {
		apiErr(w, 400, "版本超过 512KB，请直接恢复后用编辑器查看")
		return
	}
	writeJSON(w, 200, map[string]string{"content": string(b)})
}

func handleVersionRestore(w http.ResponseWriter, r *http.Request) {
	var body struct {
		ID string `json:"id"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	f := versionFile(body.ID)
	if f == "" {
		apiErr(w, 400, "版本 ID 非法")
		return
	}
	src, err := os.ReadFile(f)
	if err != nil {
		apiErr(w, 404, "版本不存在")
		return
	}
	// 找出原路径
	var target string
	for _, v := range loadVersions() {
		if v.ID == body.ID {
			target = v.Path
			break
		}
	}
	if target == "" {
		apiErr(w, 404, "版本记录不存在")
		return
	}
	// 恢复前把当前内容也快照一份，避免"恢复"变成不可逆操作
	snapshotVersion(target)
	if err := os.WriteFile(target, src, 0644); err != nil {
		apiErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]string{"ok": "1", "path": target})
}

func handleVersionDelete(w http.ResponseWriter, r *http.Request) {
	var body struct {
		ID string `json:"id"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	f := versionFile(body.ID)
	if f == "" {
		apiErr(w, 400, "版本 ID 非法")
		return
	}
	os.Remove(f)
	versionMu.Lock()
	items := loadVersions()
	kept := items[:0]
	for _, v := range items {
		if v.ID != body.ID {
			kept = append(kept, v)
		}
	}
	saveVersions(kept)
	versionMu.Unlock()
	writeJSON(w, 200, map[string]string{"ok": "1"})
}

// gcOrphanVersions 清理原文件已消失、且超过 7 天的版本快照。
// 文件被删除后版本仍占磁盘，长期会积累成无主数据。
func gcOrphanVersions() {
	versionMu.Lock()
	defer versionMu.Unlock()
	items := loadVersions()
	kept := make([]VersionItem, 0, len(items))
	now := time.Now().Unix()
	removed := 0
	for _, v := range items {
		if _, err := os.Stat(v.Path); err != nil {
			if now-v.Ts > 7*24*3600 {
				if f := versionFile(v.ID); f != "" {
					os.Remove(f)
				}
				removed++
				continue
			}
		}
		kept = append(kept, v)
	}
	if removed > 0 {
		saveVersions(kept)
	}
}

// ================= 收藏夹 =================

var favMu sync.Mutex

func favPath() string { return filepath.Join(dataDir, "favorites.json") }

func loadFavs() []string {
	b, err := os.ReadFile(favPath())
	if err != nil {
		return []string{}
	}
	var out []string
	json.Unmarshal(b, &out)
	if out == nil {
		return []string{}
	}
	return out
}

func handleFavList(w http.ResponseWriter, r *http.Request) {
	favs := loadFavs()
	type entry struct {
		Path  string `json:"path"`
		Name  string `json:"name"`
		Valid bool   `json:"valid"`
	}
	out := make([]entry, 0, len(favs))
	for _, p := range favs {
		_, err := os.Stat(p)
		out = append(out, entry{Path: p, Name: filepath.Base(p), Valid: err == nil})
	}
	writeJSON(w, 200, out)
}

func handleFavToggle(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Path string `json:"path"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	abs, err := sandboxPath(body.Path)
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	favMu.Lock()
	defer favMu.Unlock()
	favs := loadFavs()
	found := -1
	for i, p := range favs {
		if p == abs {
			found = i
			break
		}
	}
	added := false
	if found >= 0 {
		favs = append(favs[:found], favs[found+1:]...)
	} else {
		if len(favs) >= 30 {
			apiErr(w, 400, "收藏夹最多 30 项")
			return
		}
		favs = append(favs, abs)
		added = true
	}
	b, _ := json.MarshalIndent(favs, "", " ")
	os.WriteFile(favPath(), b, 0600)
	writeJSON(w, 200, map[string]interface{}{"added": added, "count": len(favs)})
}

// ================= 内容搜索 =================

type SearchHit struct {
	Path  string `json:"path"`
	Name  string `json:"name"`
	IsDir bool   `json:"is_dir"`
	Size  int64  `json:"size"`
	Line  int    `json:"line"`
	Text  string `json:"text"`
	Match string `json:"match"` // name / content
}

func looksBinary(head []byte) bool {
	for _, b := range head {
		if b == 0 {
			return true
		}
	}
	return false
}

// searchTree 在 deadline 前遍历目录，命中上限后立即返回，保证不会拖死面板
func searchTree(root, q string, suffixes []string, recursive bool, limit int, deadline time.Time, t *Task) ([]SearchHit, bool, error) {
	hits := []SearchHit{}
	truncated := false
	ql := strings.ToLower(q)
	scanned := 0

	check := func(p string, info os.FileInfo) bool {
		if time.Now().After(deadline) {
			truncated = true
			return false
		}
		if t != nil && t.canceled() {
			return false
		}
		scanned++
		if len(hits) >= limit {
			truncated = true
			return false
		}
		name := info.Name()
		isDir := info.IsDir()

		if strings.Contains(strings.ToLower(name), ql) {
			hits = append(hits, SearchHit{Path: p, Name: name, IsDir: isDir, Size: info.Size(), Match: "name"})
			if len(hits) >= limit {
				truncated = true
				return false
			}
		}
		if isDir || info.Size() > 2<<20 {
			return true
		}
		if len(suffixes) > 0 {
			ext := strings.TrimPrefix(strings.ToLower(filepath.Ext(name)), ".")
			ok := false
			for _, s := range suffixes {
				if ext == s {
					ok = true
					break
				}
			}
			if !ok {
				return true
			}
		}
		f, err := os.Open(p)
		if err != nil {
			return true
		}
		head := make([]byte, 512)
		n, _ := f.Read(head)
		if looksBinary(head[:n]) {
			f.Close()
			return true
		}
		f.Seek(0, 0)
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 64*1024), 512*1024)
		lineNo := 0
		for sc.Scan() {
			lineNo++
			line := sc.Text()
			if strings.Contains(strings.ToLower(line), ql) {
				txt := line
				if len(txt) > 200 {
					txt = txt[:200] + "…"
				}
				hits = append(hits, SearchHit{
					Path: p, Name: name, Size: info.Size(),
					Line: lineNo, Text: txt, Match: "content",
				})
				if len(hits) >= limit {
					truncated = true
					break
				}
			}
			if lineNo > 200000 {
				break
			}
		}
		f.Close()
		return len(hits) < limit
	}

	if recursive {
		filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
			if err != nil || info == nil {
				return nil
			}
			if p != root && info.IsDir() && strings.HasPrefix(info.Name(), ".") {
				return filepath.SkipDir
			}
			if !check(p, info) {
				return fmt.Errorf("stop")
			}
			return nil
		})
	} else {
		ents, err := os.ReadDir(root)
		if err != nil {
			return nil, false, err
		}
		for _, e := range ents {
			info, err := e.Info()
			if err != nil {
				continue
			}
			if !check(filepath.Join(root, e.Name()), info) {
				break
			}
		}
	}
	return hits, truncated, nil
}

func handleSearch(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Path      string `json:"path"`
		Q         string `json:"q"`
		Suffix    string `json:"suffix"`
		Sub       bool   `json:"sub"`
		Limit     int    `json:"limit"`
		TimeoutMS int    `json:"timeout_ms"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	if strings.TrimSpace(body.Q) == "" {
		apiErr(w, 400, "搜索内容不能为空")
		return
	}
	root, err := sandboxPath(body.Path)
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	if body.Limit <= 0 || body.Limit > 500 {
		body.Limit = 200
	}
	if body.TimeoutMS <= 0 || body.TimeoutMS > 30000 {
		body.TimeoutMS = 8000
	}
	var suffixes []string
	for _, s := range strings.Split(body.Suffix, ",") {
		s = strings.TrimSpace(strings.TrimPrefix(strings.ToLower(s), "."))
		if s != "" {
			suffixes = append(suffixes, s)
		}
	}
	q := body.Q
	sub := body.Sub
	limit := body.Limit
	timeout := time.Duration(body.TimeoutMS) * time.Millisecond

	t := startJob("search", "搜索 "+q, func(t *Task) error {
		t.logf("目录: %s", root)
		t.logf("条件: %q · 后缀 %v · 递归 %v", q, suffixes, sub)
		hits, truncated, err := searchTree(root, q, suffixes, sub, limit, time.Now().Add(timeout), t)
		if err != nil {
			return err
		}
		t.setResult(map[string]interface{}{
			"hits": hits, "truncated": truncated, "root": root,
			"limit": limit, "timeout_ms": body.TimeoutMS,
		})
		t.logf("命中 %d 条%s", len(hits), map[bool]string{true: "（已截断）", false: ""}[truncated])
		return nil
	})
	writeJSON(w, 200, taskSnapshot(t))
}

// ================= 设置项 =================

type fileSettings struct {
	TrashKeepDays int `json:"trash_keep_days"`
	VersionKeep   int `json:"version_keep"`
}

var settingsMu sync.Mutex

func fileSettingsPath() string { return filepath.Join(dataDir, "file-settings.json") }

func loadFileSettings() fileSettings {
	fs := fileSettings{TrashKeepDays: 7, VersionKeep: versionKeepDef}
	b, err := os.ReadFile(fileSettingsPath())
	if err == nil {
		json.Unmarshal(b, &fs)
	}
	if fs.TrashKeepDays < 0 {
		fs.TrashKeepDays = 0
	}
	if fs.VersionKeep <= 0 {
		fs.VersionKeep = versionKeepDef
	}
	return fs
}

func trashKeepDays() int  { return loadFileSettings().TrashKeepDays }
func versionKeep() int    { return loadFileSettings().VersionKeep }

func handleFileSettings(w http.ResponseWriter, r *http.Request) {
	if r.Method == "GET" {
		writeJSON(w, 200, loadFileSettings())
		return
	}
	var body fileSettings
	json.NewDecoder(r.Body).Decode(&body)
	if body.TrashKeepDays < 0 || body.TrashKeepDays > 365 {
		apiErr(w, 400, "保留天数需在 0–365 之间（0 表示不自动清理）")
		return
	}
	if body.VersionKeep < 1 || body.VersionKeep > 50 {
		apiErr(w, 400, "版本保留数需在 1–50 之间")
		return
	}
	settingsMu.Lock()
	b, _ := json.MarshalIndent(body, "", " ")
	os.WriteFile(fileSettingsPath(), b, 0600)
	settingsMu.Unlock()
	writeJSON(w, 200, body)
}

// ================= 复制 / 移动 =================

// copyFile 复制单个文件，保留权限
func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	st, err := in.Stat()
	if err != nil {
		return err
	}
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, st.Mode().Perm())
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

func copyTree(src, dst string) error {
	st, err := os.Stat(src)
	if err != nil {
		return err
	}
	if !st.IsDir() {
		return copyFile(src, dst)
	}
	return filepath.Walk(src, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, p)
		target := filepath.Join(dst, rel)
		if info.IsDir() {
			return os.MkdirAll(target, info.Mode().Perm())
		}
		return copyFile(p, target)
	})
}

func uniquePath(dst string) string {
	if _, err := os.Stat(dst); err != nil {
		return dst
	}
	dir, name := filepath.Split(dst)
	ext := filepath.Ext(name)
	base := strings.TrimSuffix(name, ext)
	for i := 1; i < 1000; i++ {
		cand := filepath.Join(dir, fmt.Sprintf("%s (%d)%s", base, i, ext))
		if _, err := os.Stat(cand); err != nil {
			return cand
		}
	}
	return dst
}

func handleCopyMove(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Paths []string `json:"paths"`
		Dest  string   `json:"dest"`
		Move  bool     `json:"move"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	if len(body.Paths) == 0 {
		apiErr(w, 400, "未选择文件")
		return
	}
	destDir, err := sandboxPath(body.Dest)
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	if st, err := os.Stat(destDir); err != nil || !st.IsDir() {
		apiErr(w, 400, "目标不是目录")
		return
	}
	done, failed := 0, []string{}
	for _, p := range body.Paths {
		abs, err := sandboxPath(p)
		if err != nil {
			failed = append(failed, p+": 路径非法")
			continue
		}
		if abs == destDir || strings.HasPrefix(destDir+"/", abs+"/") {
			failed = append(failed, filepath.Base(abs)+": 不能复制到自身或其子目录")
			continue
		}
		target := uniquePath(filepath.Join(destDir, filepath.Base(abs)))
		if body.Move {
			if err := os.Rename(abs, target); err != nil {
				if err2 := copyTree(abs, target); err2 != nil {
					failed = append(failed, filepath.Base(abs)+": "+err2.Error())
					continue
				}
				os.RemoveAll(abs)
			}
		} else {
			if err := copyTree(abs, target); err != nil {
				failed = append(failed, filepath.Base(abs)+": "+err.Error())
				continue
			}
		}
		done++
	}
	writeJSON(w, 200, map[string]interface{}{"done": done, "failed": failed})
}

// ================= 权限 =================

func handleChmod(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Path      string `json:"path"`
		Mode      string `json:"mode"` // 八进制，如 755
		Recursive bool   `json:"recursive"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	abs, err := sandboxPath(body.Path)
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	mode, err := strconv.ParseUint(body.Mode, 8, 32)
	if err != nil || mode > 0777 {
		apiErr(w, 400, "权限需为 0–777 的八进制数")
		return
	}
	perm := os.FileMode(mode)
	if st, err := os.Stat(abs); err != nil {
		apiErr(w, 404, "文件不存在")
		return
	} else if st.IsDir() && body.Recursive {
		// 递归可能改动整棵目录树，前端必须二次确认
		n := 0
		filepath.Walk(abs, func(p string, info os.FileInfo, err error) error {
			if err != nil {
				return nil
			}
			os.Chmod(p, perm)
			n++
			if n > 20000 {
				return fmt.Errorf("stop")
			}
			return nil
		})
		writeJSON(w, 200, map[string]interface{}{"ok": "1", "changed": n, "recursive": true})
		return
	}
	if err := os.Chmod(abs, perm); err != nil {
		apiErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]interface{}{"ok": "1", "changed": 1})
}

// ================= 属性 =================

func handleStat(w http.ResponseWriter, r *http.Request) {
	abs, err := sandboxPath(r.URL.Query().Get("path"))
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	st, err := os.Stat(abs)
	if err != nil {
		apiErr(w, 404, "文件不存在")
		return
	}
	var total int64
	if st.IsDir() {
		total = dirSize(abs)
	}
	writeJSON(w, 200, map[string]interface{}{
		"path": abs, "name": filepath.Base(abs), "is_dir": st.IsDir(),
		"size": st.Size(), "dir_size": total, "mode": fmt.Sprintf("%04o", st.Mode().Perm()),
		"mode_str": st.Mode().String(), "mtime": st.ModTime().Unix(),
		"owner": "termux", "versions": len(versionsFor(abs)),
	})
}

func versionsFor(abs string) []VersionItem {
	var out []VersionItem
	for _, v := range loadVersions() {
		if v.Path == abs {
			out = append(out, v)
		}
	}
	return out
}
