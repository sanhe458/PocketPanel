package main

import (
	"encoding/json"
	"fmt"
	"sync"
	"time"
)

// 非命令类任务（下载 / 压缩 / 解压 / 搜索 / 统计）的公共辅助。
// 与 cmd 任务共用 Task 结构和任务列表，前端只需按 kind 区分展示。

var jobSeqMu sync.Mutex

func newJob(kind, title string) *Task {
	t := &Task{
		ID: randHex(6), Kind: kind, Title: title, Status: "running",
		Started: time.Now().Unix(), mu: &sync.Mutex{},
	}
	registerTask(t)
	return t
}

// startJob 启动一个后台作业，panic 也不会让进程退出，而是把任务标记为失败
func startJob(kind, title string, fn func(t *Task) error) *Task {
	t := newJob(kind, title)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				t.finish(fmt.Errorf("任务异常终止: %v", r))
			}
		}()
		if err := fn(t); err != nil {
			t.finish(err)
			return
		}
		t.finish(nil)
	}()
	return t
}

func (t *Task) logf(format string, args ...interface{}) {
	line := fmt.Sprintf(format, args...)
	t.mu.Lock()
	if len(t.Output) < 200*1024 {
		t.Output += line + "\n"
	}
	t.mu.Unlock()
}

func (t *Task) setProgress(p int) {
	if p < 0 {
		p = 0
	}
	if p > 100 {
		p = 100
	}
	t.mu.Lock()
	t.Progress = p
	t.mu.Unlock()
}

func (t *Task) setResult(v interface{}) {
	b, err := json.Marshal(v)
	if err != nil {
		return
	}
	t.mu.Lock()
	t.Result = b
	t.mu.Unlock()
}

func (t *Task) finish(err error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.Status != "running" {
		return // 已被取消
	}
	t.Ended = time.Now().Unix()
	if err != nil {
		t.Status = "failed"
		t.Exit = 1
		if len(t.Output) < 200*1024 {
			t.Output += "错误: " + err.Error() + "\n"
		}
	} else {
		t.Status = "success"
		if t.Progress < 100 {
			t.Progress = 100
		}
	}
}

func (t *Task) canceled() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.Status == "canceled"
}
