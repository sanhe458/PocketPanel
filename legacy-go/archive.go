package main

import (
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// ================= 压缩 / 解压 =================
//
// 归档命令走 exec 直接传参（不经 shell），文件名里的空格与分号不会被解释。
// 任务输出逐行回传，前端可以看到具体进度。

func runCmdTask(t *Task, name string, args []string, dir string) error {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		for {
			time.Sleep(500 * time.Millisecond)
			if t.canceled() {
				cancel()
				return
			}
		}
	}()
	c := exec.CommandContext(ctx, name, args...)
	c.Dir = dir
	c.Env = append(os.Environ(), "TERM=dumb")
	pr, pw := newPipe()
	c.Stdout = pw
	c.Stderr = pw
	if err := c.Start(); err != nil {
		return err
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		buf := make([]byte, 4096)
		var acc []byte
		for {
			n, err := pr.Read(buf)
			if n > 0 {
				acc = append(acc, buf[:n]...)
				for {
					i := strings.IndexByte(string(acc), '\n')
					if i < 0 {
						break
					}
					t.logf("%s", strings.TrimRight(string(acc[:i]), "\r"))
					acc = acc[i+1:]
				}
				if len(acc) > 8192 {
					t.logf("%s", string(acc))
					acc = nil
				}
			}
			if err != nil {
				break
			}
		}
	}()
	err := c.Wait()
	pw.Close()
	<-done
	return err
}

// newPipe 隔离 os.Pipe，便于统一关闭
func newPipe() (*os.File, *os.File) {
	pr, pw, _ := os.Pipe()
	return pr, pw
}

func handleArchive(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Paths  []string `json:"paths"`
		Dest   string   `json:"dest"`
		Format string   `json:"format"` // zip / tar.gz
	}
	json.NewDecoder(r.Body).Decode(&body)
	if len(body.Paths) == 0 {
		apiErr(w, 400, "未选择文件")
		return
	}
	abs := make([]string, 0, len(body.Paths))
	parent := ""
	for _, p := range body.Paths {
		a, err := sandboxPath(p)
		if err != nil {
			apiErr(w, 400, err.Error())
			return
		}
		if _, err := os.Stat(a); err != nil {
			apiErr(w, 400, "文件不存在: "+filepath.Base(a))
			return
		}
		pa := filepath.Dir(a)
		if parent == "" {
			parent = pa
		} else if parent != pa {
			apiErr(w, 400, "请选择同一目录下的文件，跨目录请分多次压缩")
			return
		}
		abs = append(abs, a)
	}
	format := body.Format
	if format != "tar.gz" {
		format = "zip"
	}
	dest := body.Dest
	if dest == "" {
		base := "archive"
		if len(abs) == 1 {
			base = strings.TrimSuffix(filepath.Base(abs[0]), filepath.Ext(abs[0]))
		}
		ext := ".zip"
		if format == "tar.gz" {
			ext = ".tar.gz"
		}
		dest = uniquePath(filepath.Join(parent, base+ext))
	} else {
		d, err := sandboxPath(dest)
		if err != nil {
			apiErr(w, 400, err.Error())
			return
		}
		dest = d
	}
	names := make([]string, 0, len(abs))
	for _, a := range abs {
		names = append(names, filepath.Base(a))
	}
	title := fmt.Sprintf("压缩 %d 项", len(abs))
	t := startJob("archive", title, func(t *Task) error {
		t.logf("目标: %s", dest)
		t.setProgress(30)
		var err error
		if format == "zip" {
			args := append([]string{"-r", "-q", dest}, names...)
			err = runCmdTask(t, "zip", args, parent)
		} else {
			args := append([]string{"-czf", dest, "-C", parent}, names...)
			err = runCmdTask(t, "tar", args, parent)
		}
		if err != nil {
			return err
		}
		t.setProgress(100)
		st, _ := os.Stat(dest)
		var size int64
		if st != nil {
			size = st.Size()
		}
		t.setResult(map[string]interface{}{"dest": dest, "size": size})
		t.logf("完成: %s (%d 字节)", dest, size)
		return nil
	})
	writeJSON(w, 200, taskSnapshot(t))
}

func handleExtract(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Path string `json:"path"`
		Dest string `json:"dest"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	src, err := sandboxPath(body.Path)
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	st, err := os.Stat(src)
	if err != nil || st.IsDir() {
		apiErr(w, 400, "不是压缩文件")
		return
	}
	dest := body.Dest
	if dest == "" {
		dest = strings.TrimSuffix(src, filepath.Ext(src))
		if strings.HasSuffix(dest, ".tar") {
			dest = strings.TrimSuffix(dest, ".tar")
		}
		dest = uniquePath(dest)
	} else {
		d, err := sandboxPath(dest)
		if err != nil {
			apiErr(w, 400, err.Error())
			return
		}
		dest = d
	}
	name := strings.ToLower(filepath.Base(src))
	title := "解压 " + filepath.Base(src)

	t := startJob("extract", title, func(t *Task) error {
		if err := os.MkdirAll(dest, 0755); err != nil {
			return err
		}
		t.logf("解压到: %s", dest)
		t.setProgress(20)
		switch {
		case strings.HasSuffix(name, ".zip"):
			return runCmdTask(t, "unzip", []string{"-o", "-q", src, "-d", dest}, "")
		case strings.HasSuffix(name, ".tar.gz"), strings.HasSuffix(name, ".tgz"):
			return runCmdTask(t, "tar", []string{"-xzf", src, "-C", dest, "--no-same-owner"}, "")
		case strings.HasSuffix(name, ".tar"):
			return runCmdTask(t, "tar", []string{"-xf", src, "-C", dest, "--no-same-owner"}, "")
		case strings.HasSuffix(name, ".gz"):
			in, err := os.Open(src)
			if err != nil {
				return err
			}
			defer in.Close()
			zr, err := gzip.NewReader(in)
			if err != nil {
				return err
			}
			defer zr.Close()
			outPath := filepath.Join(dest, strings.TrimSuffix(filepath.Base(src), ".gz"))
			out, err := os.Create(outPath)
			if err != nil {
				return err
			}
			if _, err := io.Copy(out, zr); err != nil {
				out.Close()
				return err
			}
			out.Close()
			t.logf("解压完成: %s", outPath)
			t.setResult(map[string]interface{}{"dest": dest})
			return nil
		case strings.HasSuffix(name, ".tar.xz"), strings.HasSuffix(name, ".xz"):
			return runCmdTask(t, "tar", []string{"-xJf", src, "-C", dest, "--no-same-owner"}, "")
		default:
			return errors.New("不支持的压缩格式（zip / tar / tar.gz / tgz / gz / tar.xz）")
		}
	})
	writeJSON(w, 200, taskSnapshot(t))
}

// ================= 远程下载 =================

func guessName(u *url.URL, resp *http.Response) string {
	if cd := resp.Header.Get("Content-Disposition"); cd != "" {
		for _, part := range strings.Split(cd, ";") {
			part = strings.TrimSpace(part)
			if strings.HasPrefix(part, "filename=") {
				n := strings.Trim(strings.TrimPrefix(part, "filename="), `"'`)
				if n != "" {
					return filepath.Base(n)
				}
			}
		}
	}
	base := path.Base(u.Path)
	if base == "" || base == "/" || base == "." {
		return fmt.Sprintf("download_%d", time.Now().Unix())
	}
	return base
}

func handleRemoteDownload(w http.ResponseWriter, r *http.Request) {
	var body struct {
		URL  string `json:"url"`
		Dir  string `json:"dir"`
		Name string `json:"name"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	raw := strings.TrimSpace(body.URL)
	pu, err := url.Parse(raw)
	if err != nil || (pu.Scheme != "http" && pu.Scheme != "https") {
		apiErr(w, 400, "只支持 http / https 链接")
		return
	}
	dir, err := sandboxPath(body.Dir)
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	if st, err := os.Stat(dir); err != nil || !st.IsDir() {
		apiErr(w, 400, "目标不是目录")
		return
	}
	customName := strings.TrimSpace(body.Name)
	if customName != "" {
		if err := validName(customName); err != nil {
			apiErr(w, 400, err.Error())
			return
		}
	}

	title := "下载 " + raw
	t := startJob("download", title, func(t *Task) error {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		go func() {
			for {
				time.Sleep(400 * time.Millisecond)
				if t.canceled() {
					cancel()
					return
				}
			}
		}()
		req, _ := http.NewRequestWithContext(ctx, "GET", raw, nil)
		req.Header.Set("User-Agent", "PocketPanel/"+appVersion)
		client := &http.Client{Timeout: 0, Transport: &http.Transport{
			ResponseHeaderTimeout: 20 * time.Second,
		}}
		t.logf("GET %s", raw)
		resp, err := client.Do(req)
		if err != nil {
			return err
		}
		defer resp.Body.Close()
		if resp.StatusCode >= 400 {
			return fmt.Errorf("服务端返回 HTTP %d", resp.StatusCode)
		}
		name := customName
		if name == "" {
			name = guessName(pu, resp)
		}
		target := uniquePath(filepath.Join(dir, name))
		tmp := target + ".part"
		out, err := os.Create(tmp)
		if err != nil {
			return err
		}
		total := resp.ContentLength
		t.logf("保存到: %s（远端大小 %d 字节）", target, total)

		buf := make([]byte, 128*1024)
		var written int64
		last := time.Now()
		for {
			if t.canceled() {
				out.Close()
				os.Remove(tmp)
				return errors.New("已取消")
			}
			n, rerr := resp.Body.Read(buf)
			if n > 0 {
				if _, werr := out.Write(buf[:n]); werr != nil {
					out.Close()
					os.Remove(tmp)
					return werr
				}
				written += int64(n)
				if total > 0 {
					t.setProgress(int(written * 100 / total))
				}
				if time.Since(last) > 500*time.Millisecond {
					last = time.Now()
					if total > 0 {
						t.logf("已下载 %s / %s", humanSize(written), humanSize(total))
					} else {
						t.logf("已下载 %s", humanSize(written))
					}
				}
			}
			if rerr == io.EOF {
				break
			}
			if rerr != nil {
				out.Close()
				os.Remove(tmp)
				return rerr
			}
		}
		if err := out.Close(); err != nil {
			return err
		}
		if total > 0 && written != total {
			os.Remove(tmp)
			return fmt.Errorf("大小不一致：期望 %d，实际 %d", total, written)
		}
		if err := os.Rename(tmp, target); err != nil {
			return err
		}
		t.setProgress(100)
		t.setResult(map[string]interface{}{"path": target, "size": written})
		t.logf("完成: %s (%s)", target, humanSize(written))
		return nil
	})
	writeJSON(w, 200, taskSnapshot(t))
}

func humanSize(n int64) string {
	switch {
	case n >= 1<<30:
		return strconv.FormatFloat(float64(n)/(1<<30), 'f', 1, 64) + " GB"
	case n >= 1<<20:
		return strconv.FormatFloat(float64(n)/(1<<20), 'f', 1, 64) + " MB"
	case n >= 1<<10:
		return strconv.FormatFloat(float64(n)/(1<<10), 'f', 1, 64) + " KB"
	}
	return strconv.FormatInt(n, 10) + " B"
}
