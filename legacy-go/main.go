package main

import (
	"bufio"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"mime"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	appVersion = "0.1.0"
	prefixDir  = "/data/data/com.termux/files/usr"
	svcDir     = prefixDir + "/var/service"
)

// ---------------- 配置 ----------------

type Config struct {
	Port      int    `json:"port"`
	Username  string `json:"username"`
	Salt      string `json:"salt"`
	Hash      string `json:"hash"`
	InitPwd   string `json:"init_pwd"`
	CreatedAt int64  `json:"created_at"`
}

const defaultUser = "admin"

var (
	dataDir   string
	configPath string
	cfg       Config
	cfgMu     sync.RWMutex
)

func homeDir() string {
	if h := os.Getenv("HOME"); h != "" {
		return h
	}
	return "/data/data/com.termux/files/home"
}

func initPaths() {
	dataDir = filepath.Join(homeDir(), "pocketpanel", "data")
	os.MkdirAll(dataDir, 0700)
	configPath = filepath.Join(dataDir, "config.json")
}

func hashPwd(salt, pwd string) string {
	h := sha256.Sum256([]byte(salt + ":" + pwd))
	return hex.EncodeToString(h[:])
}

func loadOrCreateConfig() {
	b, err := os.ReadFile(configPath)
	if err == nil && json.Unmarshal(b, &cfg) == nil && cfg.Hash != "" {
		// 兼容旧配置：之前没有用户名字段
		if cfg.Username == "" {
			cfg.Username = defaultUser
			saveConfig()
		}
		return
	}
	salt := randHex(8)
	pwd := randPwd(10)
	cfg = Config{Port: 8888, Username: defaultUser, Salt: salt, Hash: hashPwd(salt, pwd), InitPwd: pwd, CreatedAt: time.Now().Unix()}
	saveConfig()
	fmt.Println("========================================")
	fmt.Println(" 首次启动，已生成登录密码")
	fmt.Println(" 用户名:", cfg.Username)
	fmt.Println(" 密  码:", pwd)
	fmt.Println(" 请登录后立即修改")
	fmt.Println("========================================")
}

func saveConfig() {
	b, _ := json.MarshalIndent(cfg, "", "  ")
	os.WriteFile(configPath, b, 0600)
}

func randHex(n int) string {
	b := make([]byte, n)
	rand.Read(b)
	return hex.EncodeToString(b)
}

func randPwd(n int) string {
	const cs = "abcdefghjkmnpqrstuvwxyz23456789"
	b := make([]byte, n)
	rand.Read(b)
	for i := range b {
		b[i] = cs[int(b[i])%len(cs)]
	}
	return string(b)
}

// ---------------- 会话 ----------------

type session struct {
	Token  string
	Expire time.Time
}

var (
	sessMu sync.Mutex
	sessions = map[string]time.Time{}
)

func newSession() string {
	t := randHex(24)
	sessMu.Lock()
	sessions[t] = time.Now().Add(24 * time.Hour)
	sessMu.Unlock()
	return t
}

func checkSession(r *http.Request) bool {
	c, err := r.Cookie("pp_token")
	if err != nil {
		return false
	}
	sessMu.Lock()
	exp, ok := sessions[c.Value]
	sessMu.Unlock()
	if !ok {
		return false
	}
	if time.Now().After(exp) {
		sessMu.Lock()
		delete(sessions, c.Value)
		sessMu.Unlock()
		return false
	}
	return true
}

// ---------------- 系统指标 ----------------

type Overview struct {
	CPU       float64   `json:"cpu"`
	Cores     int       `json:"cores"`
	Quota     int       `json:"quota"`
	MemUsed   uint64    `json:"mem_used"`
	MemTotal  uint64    `json:"mem_total"`
	DiskUsed  uint64    `json:"disk_used"`
	DiskTotal uint64    `json:"disk_total"`
	Load      []float64 `json:"load"`
	Temp      float64   `json:"temp"`
	Battery   int       `json:"battery"`
	Charging  bool      `json:"charging"`
	Uptime    float64   `json:"uptime"`
	Procs     int       `json:"procs"`
	Running   int       `json:"running"`
	CPUSource string    `json:"cpu_source"`
	Model     string    `json:"model"`
	Android   string    `json:"android"`
	Arch      string    `json:"arch"`
	GoVer     string    `json:"go"`
	Hostname  string    `json:"hostname"`
	Time      int64     `json:"time"`
}

var (
	cpuMu sync.Mutex
	cpuVal float64
	cpuProcs, cpuRunning int
	cpuCores int
	cpuAt time.Time
	battMu sync.Mutex
	battPct int
	battChg bool
	battTemp float64
	battAt time.Time
	startAt = time.Now()
	devOnce sync.Once
	devModel, devAndroid string
)

// Android 10 禁止读取 /proc/stat，只能从 top 取汇总行。
// top -b -n1 首次采样没有 CPU 行（缺少时间差数据），必须 -n2 才能拿到；
// 而 -n2 至少耗时 1 秒，不能放在请求路径里，因此改为后台协程定时采样、请求只读缓存。
func sampleCPUOnce() {
	out := sh("top -b -n2 -d 0.2 2>/dev/null")
	lines := strings.Split(out, "\n")

	// 汇总行形如 800%cpu 12%user ... 785%idle，其中 800 表示 8 核总量。
	// 但这台设备读不到 /proc/stat，toybox 的 idle 恒为满值，不能用来算使用率；
	// 进程行的 %CPU 是真实采样值，改为求和后再除以核数。
	var cpuLine, taskLine string
	lastHdr := -1
	for i, ln := range lines {
		t := strings.TrimSpace(ln)
		if strings.HasPrefix(t, "Tasks:") {
			taskLine = t
		}
		if strings.Contains(t, "%cpu") && strings.Contains(t, "%idle") && !strings.Contains(t, "PID") {
			cpuLine = t
		}
		if strings.Contains(t, "PID") && strings.Contains(t, "%CPU") && strings.Contains(t, "ARGS") {
			lastHdr = i
		}
	}

	cores := runtime.NumCPU()
	if cpuLine != "" {
		for _, x := range strings.Fields(cpuLine) {
			if strings.HasSuffix(x, "%cpu") {
				if v, err := strconv.ParseFloat(strings.TrimSuffix(x, "%cpu"), 64); err == nil && v >= 100 {
					cores = int(math.Round(v / 100))
				}
			}
		}
	}

	sum := 0.0
	if lastHdr >= 0 {
		for _, ln := range lines[lastHdr+1:] {
			f := strings.Fields(ln)
			if len(f) < 9 {
				continue
			}
			if _, err := strconv.Atoi(f[0]); err != nil {
				continue
			}
			if v, err := strconv.ParseFloat(f[8], 64); err == nil && v > 0 {
				sum += v
			}
		}
	}
	val := 0.0
	if cores > 0 {
		val = math.Round(sum/float64(cores)*10) / 10
		if val > 100 {
			val = 100
		}
	}

	procs, running := 0, 0
	if taskLine != "" {
		f := strings.Fields(taskLine)
		for i, x := range f {
			if i > 0 && x == "total," {
				procs, _ = strconv.Atoi(f[i-1])
			}
			if i > 0 && x == "running," {
				running, _ = strconv.Atoi(f[i-1])
			}
		}
	}

	cpuMu.Lock()
	cpuVal = val
	cpuCores = cores
	if procs > 0 {
		cpuProcs, cpuRunning = procs, running
	}
	cpuAt = time.Now()
	cpuMu.Unlock()
}

func cpuLoop() {
	for {
		sampleCPUOnce()
		time.Sleep(8 * time.Second)
	}
}

// cpuSample 只返回缓存值，不做阻塞调用
func cpuSample() (float64, int, int) {
	cpuMu.Lock()
	defer cpuMu.Unlock()
	return cpuVal, cpuProcs, cpuRunning
}

func cpuCoreCount() int {
	cpuMu.Lock()
	defer cpuMu.Unlock()
	if cpuCores > 0 {
		return cpuCores
	}
	return runtime.NumCPU()
}

// sysfs 不可读，电池与温度走 Termux:API
func batterySample() (int, bool, float64) {
	battMu.Lock()
	defer battMu.Unlock()
	if time.Since(battAt) < 5*time.Second {
		return battPct, battChg, battTemp
	}
	out := sh("termux-battery-status 2>/dev/null")
	if out != "" {
		var b struct {
			Percentage float64 `json:"percentage"`
			Temperature float64 `json:"temperature"`
			Plugged    string  `json:"plugged"`
		}
		if json.Unmarshal([]byte(out), &b) == nil {
			battPct = int(b.Percentage)
			battTemp = b.Temperature
			battChg = strings.Contains(b.Plugged, "PLUGGED") && !strings.Contains(b.Plugged, "UNPLUGGED")
		}
	}
	battAt = time.Now()
	return battPct, battChg, battTemp
}

func panelUptime() float64 { return time.Since(startAt).Seconds() }

func deviceInfo() (string, string) {
	devOnce.Do(func() {
		devModel = strings.TrimSpace(sh("getprop ro.product.model"))
		devAndroid = strings.TrimSpace(sh("getprop ro.build.version.release"))
	})
	return devModel, devAndroid
}



func memInfo() (used, total uint64) {
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return
	}
	defer f.Close()
	var mt, ma uint64
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		v, _ := strconv.ParseUint(fields[1], 10, 64)
		switch fields[0] {
		case "MemTotal:":
			mt = v * 1024
		case "MemAvailable:":
			ma = v * 1024
		}
	}
	total = mt
	if mt > ma {
		used = mt - ma
	}
	return
}

var (
	diskMu    sync.Mutex
	diskUsed  uint64
	diskTotal uint64
	diskAt    time.Time
)

func diskUsage(path string) (used, total uint64) {
	diskMu.Lock()
	defer diskMu.Unlock()
	// df 在 Android 上会把所有挂载点都 statfs 一遍，耗时 80ms 以上，缓存 30 秒
	if time.Since(diskAt) < 30*time.Second && diskTotal > 0 {
		return diskUsed, diskTotal
	}
	out := sh("df -k " + strconv.Quote(path) + " 2>/dev/null | tail -1")
	f := strings.Fields(out)
	if len(f) < 4 {
		return
	}
	t, err1 := strconv.ParseUint(f[1], 10, 64)
	u, err2 := strconv.ParseUint(f[2], 10, 64)
	if err1 != nil || err2 != nil {
		return
	}
	diskUsed, diskTotal, diskAt = u*1024, t*1024, time.Now()
	return diskUsed, diskTotal
}


func readFloat(path string) float64 {
	b, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	v, _ := strconv.ParseFloat(strings.TrimSpace(string(b)), 64)
	return v
}


func readFileStr(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return string(b)
}


func sh(cmd string) string {
	c := exec.Command("sh", "-c", cmd)
	c.Env = os.Environ()
	out, err := c.CombinedOutput()
	if err != nil && len(out) == 0 {
		return err.Error()
	}
	return strings.TrimSpace(string(out))
}

func getOverview() Overview {
	used, total := memInfo()
	du, dt := diskUsage(homeDir())
	cpu, procs, running := cpuSample()
	batt, chg, temp := batterySample()
	model, android := deviceInfo()
	return Overview{
		CPU:       cpu,
		Cores:     cpuCoreCount(),
		Quota:     runtime.NumCPU(),
		MemUsed:   used,
		MemTotal:  total,
		DiskUsed:  du,
		DiskTotal: dt,
		Load:      []float64{0, 0, 0},
		Temp:      temp,
		Battery:   batt,
		Charging:  chg,
		Uptime:    panelUptime(),
		Procs:     procs,
		Running:   running,
		CPUSource: "top",
		Model:     model,
		Android:   android,
		Arch:      runtime.GOARCH,
		GoVer:     runtime.Version(),
		Hostname:  "termux",
		Time:      time.Now().Unix(),
	}
}

// ---------------- 服务（runit） ----------------

type Service struct {
	Name   string `json:"name"`
	Status string `json:"status"` // run / down / fail
	PID    int    `json:"pid"`
	Uptime string `json:"uptime"`
	Raw    string `json:"raw"`
}

func listServices() []Service {
	ents, err := os.ReadDir(svcDir)
	if err != nil {
		return []Service{}
	}
	var out []Service
	for _, e := range ents {
		if !e.IsDir() || strings.HasPrefix(e.Name(), ".") {
			continue
		}
		out = append(out, serviceStatus(e.Name()))
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

func serviceStatus(name string) Service {
	s := Service{Name: name, Status: "down"}
	cmd := exec.Command("sv", "status", name)
	cmd.Env = append(os.Environ(), "SVDIR="+svcDir)
	b, _ := cmd.CombinedOutput()
	s.Raw = strings.TrimSpace(string(b))
	line := s.Raw
	switch {
	case strings.HasPrefix(line, "run:"):
		s.Status = "run"
	case strings.HasPrefix(line, "fail:"):
		s.Status = "fail"
	case strings.HasPrefix(line, "down:"):
		s.Status = "down"
	}
	if i := strings.Index(line, "(pid "); i >= 0 {
		rest := line[i+5:]
		if j := strings.Index(rest, ")"); j > 0 {
			s.PID, _ = strconv.Atoi(rest[:j])
		}
	}
	if i := strings.Index(line, ") "); i >= 0 {
		rest := line[i+2:]
		if j := strings.Index(rest, ";"); j > 0 {
			rest = rest[:j]
		}
		s.Uptime = strings.TrimSpace(rest)
	}
	return s
}

func serviceAction(name, action string) (string, error) {
	allowed := map[string]bool{"start": true, "stop": true, "restart": true, "enable": true, "disable": true}
	if !allowed[action] {
		return "", fmt.Errorf("不支持的操作: %s", action)
	}
	if strings.ContainsAny(name, "/\\ ") {
		return "", fmt.Errorf("非法服务名")
	}
	if name == "pocketpanel" && (action == "stop" || action == "restart") {
		return "", fmt.Errorf("不能从面板内部停止自身，请在终端执行 sv restart pocketpanel")
	}
	cmd := exec.Command("sv", action, name)
	cmd.Env = append(os.Environ(), "SVDIR="+svcDir)
	b, err := cmd.CombinedOutput()
	return strings.TrimSpace(string(b)), err
}

// ---------------- 文件 ----------------

type FileEntry struct {
	Name  string `json:"name"`
	IsDir bool   `json:"is_dir"`
	Size  int64  `json:"size"`
	Mode  string `json:"mode"`
	Mtime int64  `json:"mtime"`
}

func sandboxPath(p string) (string, error) {
	home := homeDir()
	if p == "" {
		p = home
	}
	if !filepath.IsAbs(p) {
		p = filepath.Join(home, p)
	}
	p = filepath.Clean(p)
	if p != home && !strings.HasPrefix(p, home+"/") {
		// 允许访问手机存储挂载点
		storage := filepath.Join(home, "storage")
		if p != storage && !strings.HasPrefix(p, storage+"/") {
			return "", fmt.Errorf("路径超出允许范围")
		}
	}
	return p, nil
}

func listDir(p string) ([]FileEntry, error) {
	abs, err := sandboxPath(p)
	if err != nil {
		return nil, err
	}
	ents, err := os.ReadDir(abs)
	if err != nil {
		return nil, err
	}
	out := make([]FileEntry, 0, len(ents))
	for _, e := range ents {
		info, err := e.Info()
		if err != nil {
			continue
		}
		out = append(out, FileEntry{
			Name:  e.Name(),
			IsDir: e.IsDir(),
			Size:  info.Size(),
			Mode:  info.Mode().String(),
			Mtime: info.ModTime().Unix(),
		})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].IsDir != out[j].IsDir {
			return out[i].IsDir
		}
		return strings.ToLower(out[i].Name) < strings.ToLower(out[j].Name)
	})
	return out, nil
}

// ---------------- 任务执行 ----------------

type Task struct {
	ID       string          `json:"id"`
	Kind     string          `json:"kind"` // cmd / download / archive / extract / search / du
	Title    string          `json:"title"`
	Cmd      string          `json:"cmd"`
	Cwd      string          `json:"cwd"`
	Status   string          `json:"status"` // running / success / failed / canceled
	Output   string          `json:"output"`
	Progress int             `json:"progress"`
	Result   json.RawMessage `json:"result,omitempty"`
	Started  int64           `json:"started"`
	Ended    int64           `json:"ended"`
	Exit     int             `json:"exit"`
	mu       *sync.Mutex
}

var (
	taskMu sync.Mutex
	tasks  = map[string]*Task{}
	taskOrder []string
)

func newTask(cmd, cwd string) *Task {
	t := &Task{ID: randHex(6), Kind: "cmd", Title: cmd, Cmd: cmd, Cwd: cwd, Status: "running",
		Started: time.Now().Unix(), mu: &sync.Mutex{}}
	registerTask(t)
	go runTask(t)
	return t
}

func registerTask(t *Task) {
	taskMu.Lock()
	tasks[t.ID] = t
	taskOrder = append([]string{t.ID}, taskOrder...)
	if len(taskOrder) > 50 {
		for _, id := range taskOrder[50:] {
			delete(tasks, id)
		}
		taskOrder = taskOrder[:50]
	}
	taskMu.Unlock()
}

func runTask(t *Task) {
	if t.Cwd == "" {
		t.Cwd = homeDir()
	}
	c := exec.Command("sh", "-c", t.Cmd)
	c.Dir = t.Cwd
	c.Env = append(os.Environ(), "TERM=dumb")
	pr, pw := io.Pipe()
	c.Stdout = pw
	c.Stderr = pw
	if err := c.Start(); err != nil {
		t.mu.Lock()
		t.Status = "failed"
		t.Output = err.Error()
		t.Ended = time.Now().Unix()
		t.mu.Unlock()
		return
	}
	go func() {
		sc := bufio.NewScanner(pr)
		sc.Buffer(make([]byte, 64*1024), 1024*1024)
		for sc.Scan() {
			t.mu.Lock()
			if len(t.Output) < 200*1024 {
				t.Output += sc.Text() + "\n"
			}
			t.mu.Unlock()
		}
	}()
	err := c.Wait()
	pw.Close()
	t.mu.Lock()
	t.Ended = time.Now().Unix()
	if err != nil {
		t.Status = "failed"
		if ee, ok := err.(*exec.ExitError); ok {
			t.Exit = ee.ExitCode()
		} else {
			t.Exit = -1
		}
	} else {
		t.Status = "success"
	}
	t.mu.Unlock()
}

func taskSnapshot(t *Task) Task {
	t.mu.Lock()
	defer t.mu.Unlock()
	// 逐字段拷贝：Task 内含互斥锁，直接按值复制会让 vet 报 copylocks，
	// 且副本里的锁与原文无关，后续加锁等于没加
	return Task{
		ID: t.ID, Kind: t.Kind, Title: t.Title, Cmd: t.Cmd, Cwd: t.Cwd, Status: t.Status,
		Output: t.Output, Progress: t.Progress, Result: t.Result,
		Started: t.Started, Ended: t.Ended, Exit: t.Exit,
	}
}

func listTasks() []Task {
	taskMu.Lock()
	ids := append([]string{}, taskOrder...)
	taskMu.Unlock()
	out := make([]Task, 0, len(ids))
	for _, id := range ids {
		taskMu.Lock()
		t := tasks[id]
		taskMu.Unlock()
		if t != nil {
			out = append(out, taskSnapshot(t))
		}
	}
	return out
}

// ---------------- HTTP ----------------

func writeJSON(w http.ResponseWriter, code int, v interface{}) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

func apiErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

func authWrap(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !checkSession(r) {
			apiErr(w, 401, "未登录")
			return
		}
		h(w, r)
	}
}

func handleLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		apiErr(w, 405, "method not allowed")
		return
	}
	var body struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	cfgMu.RLock()
	user, salt, hash := cfg.Username, cfg.Salt, cfg.Hash
	cfgMu.RUnlock()
	if user == "" {
		user = defaultUser
	}
	// 用户名与密码分别做等时比较，避免通过响应时间判断哪一项错误
	userOK := subtle.ConstantTimeCompare([]byte(body.Username), []byte(user)) == 1
	pwdOK := subtle.ConstantTimeCompare([]byte(hashPwd(salt, body.Password)), []byte(hash)) == 1
	if !userOK || !pwdOK {
		time.Sleep(300 * time.Millisecond)
		apiErr(w, 401, "用户名或密码错误")
		return
	}
	tok := newSession()
	http.SetCookie(w, &http.Cookie{
		Name: "pp_token", Value: tok, Path: "/", HttpOnly: true,
		MaxAge: 86400, SameSite: http.SameSiteLaxMode,
	})
	writeJSON(w, 200, map[string]string{"token": tok, "username": user})
}

// 当前会话信息：用户名与账户创建时间
func handleSession(w http.ResponseWriter, r *http.Request) {
	cfgMu.RLock()
	user, created := cfg.Username, cfg.CreatedAt
	cfgMu.RUnlock()
	if user == "" {
		user = defaultUser
	}
	writeJSON(w, 200, map[string]interface{}{
		"username": user, "created_at": created, "version": appVersion,
	})
}

func handleLogout(w http.ResponseWriter, r *http.Request) {
	if c, err := r.Cookie("pp_token"); err == nil {
		sessMu.Lock()
		delete(sessions, c.Value)
		sessMu.Unlock()
	}
	http.SetCookie(w, &http.Cookie{Name: "pp_token", Value: "", Path: "/", MaxAge: -1})
	writeJSON(w, 200, map[string]string{"ok": "1"})
}

func handleChangePwd(w http.ResponseWriter, r *http.Request) {
	var body struct {
		OldPassword string `json:"old_password"`
		Username    string `json:"username"`
		Password    string `json:"password"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		apiErr(w, 400, "请求格式错误")
		return
	}
	cfgMu.RLock()
	user, salt, hash := cfg.Username, cfg.Salt, cfg.Hash
	cfgMu.RUnlock()
	if user == "" {
		user = defaultUser
	}
	if subtle.ConstantTimeCompare([]byte(hashPwd(salt, body.OldPassword)), []byte(hash)) != 1 {
		time.Sleep(300 * time.Millisecond)
		apiErr(w, 403, "当前密码错误")
		return
	}
	newUser := strings.TrimSpace(body.Username)
	if newUser == "" {
		newUser = user
	}
	if len([]rune(newUser)) < 2 || len([]rune(newUser)) > 32 {
		apiErr(w, 400, "用户名需为 2–32 个字符")
		return
	}
	for _, c := range newUser {
		if c == ' ' || c == '\t' || c == ':' || c == '/' || c == '\\' {
			apiErr(w, 400, "用户名不能包含空格、冒号或斜杠")
			return
		}
	}
	if body.Password != "" && len(body.Password) < 6 {
		apiErr(w, 400, "新密码至少 6 位")
		return
	}
	ns, nh := salt, hash
	if body.Password != "" {
		ns = randHex(8)
		nh = hashPwd(ns, body.Password)
	}
	cfgMu.Lock()
	cfg.Username, cfg.Salt, cfg.Hash, cfg.InitPwd = newUser, ns, nh, ""
	saveConfig()
	cfgMu.Unlock()
	// 账户变更后让所有旧会话失效，当前请求重新签发一个
	sessMu.Lock()
	sessions = map[string]time.Time{}
	sessMu.Unlock()
	tok := newSession()
	http.SetCookie(w, &http.Cookie{
		Name: "pp_token", Value: tok, Path: "/", HttpOnly: true,
		MaxAge: 86400, SameSite: http.SameSiteLaxMode,
	})
	writeJSON(w, 200, map[string]string{"ok": "1", "username": newUser, "token": tok})
}

func handleOverview(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, getOverview())
}

func handleServices(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, listServices())
}

func handleServiceAction(w http.ResponseWriter, r *http.Request) {
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/services/"), "/")
	if len(parts) != 2 {
		apiErr(w, 400, "bad path")
		return
	}
	out, err := serviceAction(parts[0], parts[1])
	if err != nil {
		apiErr(w, 500, strings.TrimSpace(out+" "+err.Error()))
		return
	}
	writeJSON(w, 200, map[string]string{"ok": out})
}

func handleFiles(w http.ResponseWriter, r *http.Request) {
	p := r.URL.Query().Get("path")
	ents, err := listDir(p)
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	abs, _ := sandboxPath(p)
	writeJSON(w, 200, map[string]interface{}{"path": abs, "entries": ents})
}

func handleFileRead(w http.ResponseWriter, r *http.Request) {
	abs, err := sandboxPath(r.URL.Query().Get("path"))
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	st, err := os.Stat(abs)
	if err != nil || st.IsDir() {
		apiErr(w, 400, "不是文件")
		return
	}
	// 下载与原始输出：不经过 JSON，直接流式返回，支持 Range
	if r.URL.Query().Get("download") == "1" || r.URL.Query().Get("raw") == "1" {
		f, err := os.Open(abs)
		if err != nil {
			apiErr(w, 500, err.Error())
			return
		}
		defer f.Close()
		if r.URL.Query().Get("download") == "1" {
			w.Header().Set("Content-Type", "application/octet-stream")
			w.Header().Set("Content-Disposition", "attachment; filename*=UTF-8''"+urlEncode(st.Name()))
		} else if ct := mime.TypeByExtension(filepath.Ext(abs)); ct != "" {
			w.Header().Set("Content-Type", ct)
		}
		http.ServeContent(w, r, st.Name(), st.ModTime(), f)
		return
	}
	if st.Size() > 512*1024 {
		apiErr(w, 400, "文件超过 512KB，请用其他方式编辑")
		return
	}
	b, err := os.ReadFile(abs)
	if err != nil {
		apiErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]string{"path": abs, "content": string(b)})
}

func handleFileWrite(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Path    string `json:"path"`
		Content string `json:"content"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	abs, err := sandboxPath(body.Path)
	if err != nil {
		apiErr(w, 400, err.Error())
		return
	}
	if len(body.Content) > 8<<20 {
		apiErr(w, 400, "内容超过 8MB，请用其他方式上传")
		return
	}
	// 覆盖前先留一份旧内容，历史版本可恢复
	verID := snapshotVersion(abs)
	if err := os.WriteFile(abs, []byte(body.Content), 0644); err != nil {
		apiErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]interface{}{"ok": "1", "version": verID})
}

func handleExec(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Cmd string `json:"cmd"`
		Cwd string `json:"cwd"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	if strings.TrimSpace(body.Cmd) == "" {
		apiErr(w, 400, "命令为空")
		return
	}
	t := newTask(body.Cmd, body.Cwd)
	writeJSON(w, 200, taskSnapshot(t))
}

func handleTaskGet(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/api/exec/")
	taskMu.Lock()
	t := tasks[id]
	taskMu.Unlock()
	if t == nil {
		apiErr(w, 404, "任务不存在")
		return
	}
	writeJSON(w, 200, taskSnapshot(t))
}

func handleTasks(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, listTasks())
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]interface{}{
		"ok": true, "version": appVersion, "go": runtime.Version(),
	})
}

func main() {
	initPaths()
	loadOrCreateConfig()
	cfgMu.RLock()
	port := cfg.Port
	cfgMu.RUnlock()
	if p := os.Getenv("PP_PORT"); p != "" {
		if v, err := strconv.Atoi(p); err == nil {
			port = v
		}
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", handleHealth)
	mux.HandleFunc("/api/login", handleLogin)
	mux.HandleFunc("/api/logout", handleLogout)
	mux.HandleFunc("/api/session", authWrap(handleSession))

	mux.HandleFunc("/api/overview", authWrap(handleOverview))
	mux.HandleFunc("/api/services", authWrap(handleServices))
	mux.HandleFunc("/api/services/", authWrap(handleServiceAction))
	mux.HandleFunc("/api/files", authWrap(handleFiles))
	mux.HandleFunc("/api/file", func(w http.ResponseWriter, r *http.Request) {
		if !checkSession(r) {
			apiErr(w, 401, "未登录")
			return
		}
		switch r.Method {
		case http.MethodPut, http.MethodPost:
			handleFileWrite(w, r)
		case http.MethodDelete:
			handleDelete(w, r)
		default:
			handleFileRead(w, r)
		}
	})
	mux.HandleFunc("/api/exec", authWrap(handleExec))
	mux.HandleFunc("/api/exec/", authWrap(handleTaskGet))
	mux.HandleFunc("/api/tasks", authWrap(handleTasks))
	mux.HandleFunc("/api/file/mkdir", authWrap(handleMkdir))
	mux.HandleFunc("/api/file/touch", authWrap(handleTouch))
	mux.HandleFunc("/api/file/rename", authWrap(handleRename))
	mux.HandleFunc("/api/processes", authWrap(handleProcesses))
	mux.HandleFunc("/api/processes/", authWrap(handleKill))
	mux.HandleFunc("/api/network", authWrap(handleNetwork))
	mux.HandleFunc("/api/logs", authWrap(handleLogs))
	mux.HandleFunc("/api/logs/", authWrap(handleLogTail))
	mux.HandleFunc("/api/disk", authWrap(handleDiskUsage))

	// 文件管理扩展（M05）
	mux.HandleFunc("/api/file/chmod", authWrap(handleChmod))
	mux.HandleFunc("/api/file/stat", authWrap(handleStat))
	mux.HandleFunc("/api/file/copy", authWrap(handleCopyMove))
	mux.HandleFunc("/api/trash", authWrap(handleTrashList))
	mux.HandleFunc("/api/trash/restore", authWrap(handleTrashRestore))
	mux.HandleFunc("/api/trash/purge", authWrap(handleTrashPurge))
	mux.HandleFunc("/api/versions", authWrap(handleVersionList))
	mux.HandleFunc("/api/versions/read", authWrap(handleVersionRead))
	mux.HandleFunc("/api/versions/restore", authWrap(handleVersionRestore))
	mux.HandleFunc("/api/versions/delete", authWrap(handleVersionDelete))
	mux.HandleFunc("/api/favorites", authWrap(handleFavList))
	mux.HandleFunc("/api/favorites/toggle", authWrap(handleFavToggle))
	mux.HandleFunc("/api/search", authWrap(handleSearch))
	mux.HandleFunc("/api/archive", authWrap(handleArchive))
	mux.HandleFunc("/api/extract", authWrap(handleExtract))
	mux.HandleFunc("/api/download/remote", authWrap(handleRemoteDownload))
	mux.HandleFunc("/api/file-settings", authWrap(handleFileSettings))
	mux.HandleFunc("/api/upload/init", authWrap(handleUploadInit))
	mux.HandleFunc("/api/upload/", authWrap(handleUploadRouter))
	mux.HandleFunc("/api/shares", authWrap(handleShareCreate))
	mux.HandleFunc("/api/shares/list", authWrap(handleShareList))
	mux.HandleFunc("/api/shares/revoke", authWrap(handleShareRevoke))
	// 公开路由：不走会话校验，由提取码与有效期保护
	mux.HandleFunc("/api/public/share/", handlePublicShare)
	mux.HandleFunc("/s/", handleSharePage)
	mux.HandleFunc("/api/passwd", authWrap(handleChangePwd))

	mux.Handle("/", http.FileServer(webFS))

	go cpuLoop()
	go func() {
		autoCleanTrash()
		cleanUploads()
		gcOrphanVersions()
		for {
			time.Sleep(time.Hour)
			autoCleanTrash()
			cleanUploads()
			gcOrphanVersions()
		}
	}()

	addr := fmt.Sprintf("0.0.0.0:%d", port)
	fmt.Printf("PocketPanel %s 监听 %s\n", appVersion, addr)
	fmt.Printf("本机访问: http://127.0.0.1:%d\n", port)
	if ip := localIP(); ip != "" {
		fmt.Printf("局域网:   http://%s:%d\n", ip, port)
	}
	srv := &http.Server{
		Addr:              addr,
		Handler:           logMiddleware(mux),
		ReadHeaderTimeout: 10 * time.Second,
	}
	if err := srv.ListenAndServe(); err != nil {
		fmt.Println("启动失败:", err)
		os.Exit(1)
	}
}

func localIP() string {
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return ""
	}
	for _, a := range addrs {
		if ipn, ok := a.(*net.IPNet); ok && !ipn.IP.IsLoopback() {
			if ip4 := ipn.IP.To4(); ip4 != nil && strings.HasPrefix(ip4.String(), "192.168.") {
				return ip4.String()
			}
		}
	}
	for _, a := range addrs {
		if ipn, ok := a.(*net.IPNet); ok && !ipn.IP.IsLoopback() {
			if ip4 := ipn.IP.To4(); ip4 != nil {
				return ip4.String()
			}
		}
	}
	return ""
}

func logMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/api/") {
			fmt.Printf("%s %s %s\n", time.Now().Format("15:04:05"), r.Method, r.URL.Path)
		}
		next.ServeHTTP(w, r)
	})
}
