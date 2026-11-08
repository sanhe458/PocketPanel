package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// ---------------- 文件操作（新建 / 重命名 / 删除） ----------------

func validName(name string) error {
	if name == "" {
		return fmt.Errorf("名称不能为空")
	}
	if name == "." || name == ".." {
		return fmt.Errorf("名称非法")
	}
	if strings.ContainsAny(name, "/\\\x00") {
		return fmt.Errorf("名称不能包含斜杠")
	}
	return nil
}

func handleMkdir(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Path string `json:"path"`
		Name string `json:"name"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	if err := validName(body.Name); err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	base, err := sandboxPath(body.Path)
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	target := filepath.Join(base, body.Name)
	if err := os.Mkdir(target, 0755); err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	writeJSON(w, 200, map[string]string{"ok": "1", "path": target})
}

func handleTouch(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Path string `json:"path"`
		Name string `json:"name"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	if err := validName(body.Name); err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	base, err := sandboxPath(body.Path)
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	target := filepath.Join(base, body.Name)
	f, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0644)
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	f.Close()
	writeJSON(w, 200, map[string]string{"ok": "1", "path": target})
}

func handleRename(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Path string `json:"path"`
		Name string `json:"name"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	if err := validName(body.Name); err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	abs, err := sandboxPath(body.Path)
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	parent := filepath.Dir(abs)
	if parent == abs {
		apiErr(w, 400, "不能重命名根目录")
		return
	}
	target := filepath.Join(parent, body.Name)
	if _, err := os.Stat(target); err == nil {
		apiErr(w, 400, "目标已存在")
		return
	}
	if err := os.Rename(abs, target); err != nil {
		apiErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]string{"ok": "1", "path": target})
}

// 删除走回收站，可通过 ?permanent=1 跳过（回收站内的永久删除用另一个接口）
func handleDelete(w http.ResponseWriter, r *http.Request) {
	abs, err := sandboxPath(r.URL.Query().Get("path"))
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	if abs == homeDir() {
		apiErr(w, 400, "不能删除主目录")
		return
	}
	if abs == dataDir || strings.HasPrefix(abs, dataDir+"/") {
		apiErr(w, 400, "面板数据目录不可删除")
		return
	}
	st, err := os.Stat(abs)
	if err != nil {
		apiErr(w, 404, "文件不存在")
		return
	}
	if r.URL.Query().Get("permanent") == "1" {
		if err := os.RemoveAll(abs); err != nil {
			apiErr(w, 500, err.Error())
			return
		}
		writeJSON(w, 200, map[string]interface{}{"ok": "1", "trashed": false})
		return
	}
	id, err := moveToTrash(abs, st)
	if err != nil {
		apiErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]interface{}{"ok": "1", "trashed": true, "id": id, "name": st.Name()})
}

// ---------------- 进程 ----------------

type Proc struct {
	PID       int    `json:"pid"`
	PPID      int    `json:"ppid"`
	RSS       uint64 `json:"rss"`
	Stat      string `json:"stat"`
	Command   string `json:"command"`
	Protected bool   `json:"protected"`
	Reason    string `json:"reason,omitempty"`
}

// 这些进程被杀会导致服务链断裂或面板自身失联
func procGuard(pid int, cmd string) (bool, string) {
	if pid <= 1 {
		return true, "系统进程"
	}
	if pid == os.Getpid() {
		return true, "面板自身"
	}
	base := filepath.Base(strings.Fields(cmd + " ")[0])
	switch base {
	case "runsvdir", "runsv", "svlogd", "runit":
		return true, "runit 监督进程"
	}
	return false, ""
}

func parsePS() []Proc {
	out := sh("ps -A -o pid,ppid,rss,stat,args 2>/dev/null")
	lines := strings.Split(out, "\n")
	procs := make([]Proc, 0, len(lines))
	for i, ln := range lines {
		if i == 0 || strings.TrimSpace(ln) == "" {
			continue
		}
		f := strings.Fields(ln)
		if len(f) < 5 {
			continue
		}
		pid, err1 := strconv.Atoi(f[0])
		ppid, err2 := strconv.Atoi(f[1])
		rss, err3 := strconv.ParseUint(f[2], 10, 64)
		if err1 != nil || err2 != nil || err3 != nil {
			continue
		}
		cmd := strings.Join(f[4:], " ")
		prot, reason := procGuard(pid, cmd)
		procs = append(procs, Proc{
			PID: pid, PPID: ppid, RSS: rss * 1024,
			Stat: f[3], Command: cmd, Protected: prot, Reason: reason,
		})
	}
	sort.Slice(procs, func(i, j int) bool { return procs[i].RSS > procs[j].RSS })
	return procs
}

func handleProcesses(w http.ResponseWriter, r *http.Request) {
	procs := parsePS()
	var total uint64
	for _, p := range procs {
		total += p.RSS
	}
	writeJSON(w, 200, map[string]interface{}{
		"list": procs, "count": len(procs), "rss_total": total,
	})
}

func handleKill(w http.ResponseWriter, r *http.Request) {
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/processes/"), "/")
	if len(parts) != 2 {
		apiErr(w, 400, "bad path")
		return
	}
	pid, err := strconv.Atoi(parts[0])
	if err != nil || pid <= 0 {
		apiErr(w, 400, "PID 非法")
		return
	}
	var cmdline string
	if b, err := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", pid)); err == nil {
		cmdline = strings.ReplaceAll(string(b), "\x00", " ")
	}
	if prot, reason := procGuard(pid, cmdline); prot {
		apiErr(w, 403, "受保护进程（"+reason+"）")
		return
	}
	sig := syscall.SIGTERM
	if parts[1] == "kill9" {
		sig = syscall.SIGKILL
	}
	if err := syscall.Kill(pid, sig); err != nil {
		apiErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]string{"ok": "1"})
}

// ---------------- 网络 ----------------

type Iface struct {
	Name  string   `json:"name"`
	Addr  string   `json:"addr"`
	MAC   string   `json:"mac"`
	Flags string   `json:"flags"`
	MTU   string   `json:"mtu"`
	Addrs []string `json:"addrs"`
}

type PortState struct {
	Port int    `json:"port"`
	Open bool   `json:"open"`
	MS   int64  `json:"ms"`
	Hint string `json:"hint"`
}

var portHints = map[int]string{
	22: "SSH", 80: "HTTP", 443: "HTTPS", 3000: "Node", 3306: "MySQL",
	5000: "Flask", 5432: "PostgreSQL", 6379: "Redis", 7000: "frp",
	8000: "HTTP", 8022: "sshd", 8080: "HTTP", 8081: "HTTP", 8888: "PocketPanel",
	9000: "Portainer", 9090: "Prometheus", 11211: "Memcached", 27017: "MongoDB",
	2181: "ZooKeeper", 4000: "HTTP",
}

var probePorts = []int{22, 80, 443, 3000, 3306, 4000, 5000, 5432, 6379,
	7000, 8000, 8022, 8080, 8081, 8888, 9000, 9090, 11211, 2181, 27017}

func parseIfconfig() []Iface {
	out := sh("ifconfig 2>/dev/null")
	var list []Iface
	var cur *Iface
	for _, ln := range strings.Split(out, "\n") {
		if ln == "" {
			continue
		}
		if ln[0] != ' ' && ln[0] != '\t' {
			name := ln
			if i := strings.Index(name, ":"); i > 0 {
				name = name[:i]
			}
			name = strings.Fields(name + " x")[0]
			list = append(list, Iface{Name: name})
			cur = &list[len(list)-1]
			for _, flag := range strings.Fields(ln) {
				if strings.HasPrefix(flag, "flags=") {
					cur.Flags = flag[6:]
				}
				if strings.HasPrefix(flag, "mtu ") {
					cur.MTU = strings.TrimPrefix(flag, "mtu ")
				}
			}
			if i := strings.Index(ln, "mtu "); i > 0 {
				f := strings.Fields(ln[i:])
				if len(f) > 1 {
					cur.MTU = f[1]
				}
			}
			continue
		}
		if cur == nil {
			continue
		}
		f := strings.Fields(ln)
		if len(f) >= 2 {
			switch f[0] {
			case "inet":
				cur.Addr = f[1]
				cur.Addrs = append(cur.Addrs, f[1])
			case "inet6":
				cur.Addrs = append(cur.Addrs, f[1])
			case "ether":
				cur.MAC = f[1]
			}
		}
	}
	var real []Iface
	for _, i := range list {
		if i.Name == "lo" || i.Name == "" {
			continue
		}
		real = append(real, i)
	}
	return real
}

func probePortsRun() []PortState {
	res := make([]PortState, len(probePorts))
	var wg sync.WaitGroup
	for i, p := range probePorts {
		wg.Add(1)
		go func(i, p int) {
			defer wg.Done()
			st := PortState{Port: p, Hint: portHints[p]}
			start := time.Now()
			conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", p), 260*time.Millisecond)
			st.MS = time.Since(start).Milliseconds()
			if err == nil {
				st.Open = true
				conn.Close()
			}
			res[i] = st
		}(i, p)
	}
	wg.Wait()
	return res
}

func handleNetwork(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]interface{}{
		"ifaces":    parseIfconfig(),
		"ports":     probePortsRun(),
		"lan":       localIP(),
		"probed_at": time.Now().Unix(),
	})
}

// ---------------- 日志 ----------------

type LogInfo struct {
	Name    string `json:"name"`
	Size    int64  `json:"size"`
	ModTime int64  `json:"mod_time"`
}

func logDir() string { return prefixDir + "/var/log/sv" }

func handleLogs(w http.ResponseWriter, r *http.Request) {
	ents, err := os.ReadDir(logDir())
	if err != nil {
		writeJSON(w, 200, []LogInfo{})
		return
	}
	out := make([]LogInfo, 0, len(ents))
	for _, e := range ents {
		if !e.IsDir() {
			continue
		}
		st, err := os.Stat(filepath.Join(logDir(), e.Name(), "current"))
		if err != nil {
			out = append(out, LogInfo{Name: e.Name()})
			continue
		}
		out = append(out, LogInfo{Name: e.Name(), Size: st.Size(), ModTime: st.ModTime().Unix()})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	writeJSON(w, 200, out)
}

func handleLogTail(w http.ResponseWriter, r *http.Request) {
	name := strings.TrimPrefix(r.URL.Path, "/api/logs/")
	if err := validName(name); err != nil {
		apiErr(w, 400, "服务名非法")
		return
	}
	// 只允许读取 sv 日志目录下的已知服务，防止路径穿越
	path := filepath.Join(logDir(), name, "current")
	if !strings.HasPrefix(path, logDir()+string(os.PathSeparator)) {
		apiErr(w, 400, "路径非法")
		return
	}
	lines := 200
	if v, err := strconv.Atoi(r.URL.Query().Get("lines")); err == nil && v > 0 && v <= 2000 {
		lines = v
	}
	f, err := os.Open(path)
	if err != nil {
		apiErr(w, 404, "日志不存在")
		return
	}
	defer f.Close()
	st, _ := f.Stat()
	const maxRead = 256 * 1024
	start := int64(0)
	if st.Size() > maxRead {
		start = st.Size() - maxRead
	}
	f.Seek(start, io.SeekStart)
	b, _ := io.ReadAll(f)
	txt := string(b)
	all := strings.Split(strings.TrimRight(txt, "\n"), "\n")
	if len(all) > lines {
		all = all[len(all)-lines:]
	}
	// svlogd 会在每行前加 TAI64N 时间戳，去掉后更易读
	for i, ln := range all {
		if len(ln) > 25 && ln[4] == '-' && ln[7] == '-' && ln[10] == '_' {
			if j := strings.Index(ln, " "); j > 0 && j < 32 {
				all[i] = ln[j+1:]
			}
		}
	}
	writeJSON(w, 200, map[string]interface{}{
		"name": name, "lines": all, "size": st.Size(), "truncated": start > 0,
	})
}

// ---------------- 磁盘占用（按目录） ----------------

type DirUse struct {
	Path string `json:"path"`
	Size uint64 `json:"size"`
}

func handleDiskUsage(w http.ResponseWriter, r *http.Request) {
	home := homeDir()
	names := []string{"pocketpanel", "storage", ".termux", ".cache", ".local", "storage/shared"}
	var out []DirUse
	for _, n := range names {
		p := filepath.Join(home, n)
		if _, err := os.Stat(p); err != nil {
			continue
		}
		o := sh("du -sk " + strconv.Quote(p) + " 2>/dev/null | tail -1")
		f := strings.Fields(o)
		if len(f) < 1 {
			continue
		}
		kb, err := strconv.ParseUint(f[0], 10, 64)
		if err != nil {
			continue
		}
		out = append(out, DirUse{Path: n, Size: kb * 1024})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Size > out[j].Size })
	writeJSON(w, 200, out)
}
