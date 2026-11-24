package relay

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/fsnotify/fsnotify"
)

type watchedFile struct {
	path, resolved                    string
	expires, dirty, firstDirty, retry time.Time
	info                              os.FileInfo
	pending, busy                     bool
	cancel                            context.CancelFunc
}
type watchResult struct {
	entry *watchedFile
	err   error
}
type fileMonitor struct {
	ctx     context.Context
	cancel  context.CancelFunc
	client  *Client
	base    string
	updates chan []interface{}
	results chan watchResult
	done    chan struct{}
	mu      sync.Mutex
	active  map[string]*Task
}

func newFileMonitor(ctx context.Context, client *Client, base string) *fileMonitor {
	ctx, cancel := context.WithCancel(ctx)
	m := &fileMonitor{ctx: ctx, cancel: cancel, client: client, base: base, updates: make(chan []interface{}, 1), results: make(chan watchResult, 1), done: make(chan struct{}), active: map[string]*Task{}}
	go m.run()
	return m
}
func (m *fileMonitor) close() { m.cancel(); <-m.done }
func (m *fileMonitor) update(values []interface{}) {
	select {
	case m.updates <- values:
	default:
		select {
		case <-m.updates:
		default:
		}
		select {
		case m.updates <- values:
		default:
		}
	}
}
func (m *fileMonitor) progress() []M {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := []M{}
	for _, t := range m.active {
		out = append(out, t.progress())
	}
	return out
}
func (m *fileMonitor) read(ctx context.Context, entry *watchedFile) error {
	raw, _, err := m.client.requestContext(ctx, "POST", "/api/device/files/refresh", M{"path": entry.path})
	if err != nil {
		return err
	}
	var reply M
	if err = json.Unmarshal(raw, &reply); err != nil {
		return err
	}
	job := obj(reply["job"])
	if len(job) == 0 {
		return &APIError{409, "文件读取已在执行"}
	}
	t := newTask(job)
	defer t.cancel()
	t.ctx = ctx
	t.client = m.client
	m.mu.Lock()
	m.active[str(job["id"])] = t
	m.mu.Unlock()
	defer func() { m.mu.Lock(); delete(m.active, str(job["id"])); m.mu.Unlock() }()
	t.run([]string{m.base}, false)
	return t.report()
}
func watchPath(value, base string) string {
	path := expand(value)
	if !filepath.IsAbs(path) {
		path = filepath.Join(base, path)
	}
	path, _ = filepath.Abs(path)
	return path
}
func inspectWatched(entry *watchedFile, base string, force bool) {
	path := watchPath(entry.path, base)
	resolved, _ := filepath.EvalSymlinks(path)
	info, _ := os.Stat(path)
	changed := (entry.info == nil) != (info == nil) || entry.resolved != resolved
	if entry.info != nil && info != nil {
		changed = changed || !os.SameFile(entry.info, info) || !entry.info.ModTime().Equal(info.ModTime()) || entry.info.Size() != info.Size() || entry.info.Mode() != info.Mode()
	}
	entry.info = info
	entry.resolved = resolved
	if changed || force {
		now := time.Now()
		if !entry.pending {
			entry.firstDirty = now
		}
		entry.pending = true
		entry.dirty = now
	}
}
func (m *fileMonitor) run() {
	defer close(m.done)
	watcher, _ := fsnotify.NewWatcher()
	var events <-chan fsnotify.Event
	var errors <-chan error
	if watcher != nil {
		defer watcher.Close()
		events = watcher.Events
		errors = watcher.Errors
	}
	entries := map[string]*watchedFile{}
	directories := map[string]bool{}
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	lastScan := time.Time{}
	workers := 0
	syncDirectories := func() {
		if watcher == nil {
			return
		}
		wanted := map[string]bool{}
		for _, entry := range entries {
			wanted[filepath.Dir(watchPath(entry.path, m.base))] = true
			if entry.resolved != "" {
				wanted[filepath.Dir(entry.resolved)] = true
			}
		}
		for dir := range directories {
			if !wanted[dir] {
				watcher.Remove(dir)
				delete(directories, dir)
			}
		}
		for dir := range wanted {
			if !directories[dir] && watcher.Add(dir) == nil {
				directories[dir] = true
			}
		}
	}
	defer func() {
		for _, e := range entries {
			if e.cancel != nil {
				e.cancel()
			}
		}
	}()
	for {
		select {
		case <-m.ctx.Done():
			return
		case values := <-m.updates:
			now := time.Now()
			keep := map[string]bool{}
			for _, v := range values {
				item := obj(v)
				path := str(item["path"])
				ttl := num(item["ttl"])
				if path == "" || ttl <= 0 {
					continue
				}
				if ttl > 45 {
					ttl = 45
				}
				keep[path] = true
				entry := entries[path]
				if entry == nil {
					entry = &watchedFile{path: path}
					entries[path] = entry
					inspectWatched(entry, m.base, true)
				}
				entry.expires = now.Add(time.Duration(ttl * float64(time.Second)))
			}
			for path, entry := range entries {
				if !keep[path] {
					if entry.cancel != nil {
						entry.cancel()
					}
					delete(entries, path)
				}
			}
			syncDirectories()
		case event, ok := <-events:
			if !ok {
				events = nil
				continue
			}
			if event.Op&(fsnotify.Write|fsnotify.Create|fsnotify.Rename|fsnotify.Remove|fsnotify.Chmod) == 0 {
				continue
			}
			for _, entry := range entries {
				if event.Name == watchPath(entry.path, m.base) || event.Name == entry.resolved {
					inspectWatched(entry, m.base, true)
				}
			}
		case _, ok := <-errors:
			if !ok {
				errors = nil
				continue
			}
			lastScan = time.Time{}
		case result := <-m.results:
			workers--
			entry := result.entry
			entry.busy = false
			entry.cancel = nil
			if result.err != nil {
				entry.pending = true
				entry.retry = time.Now().Add(3 * time.Second)
			}
		case now := <-ticker.C:
			scan := now.Sub(lastScan) >= 2*time.Second
			for path, entry := range entries {
				if now.After(entry.expires) {
					if entry.cancel != nil {
						entry.cancel()
					}
					delete(entries, path)
					continue
				}
				if scan {
					inspectWatched(entry, m.base, false)
				}
				if entry.pending && !entry.busy && workers < 2 && !now.Before(entry.retry) && (now.Sub(entry.dirty) >= 300*time.Millisecond || now.Sub(entry.firstDirty) >= 2*time.Second) {
					entry.pending = false
					entry.busy = true
					workers++
					ctx, cancel := context.WithTimeout(m.ctx, 45*time.Second)
					entry.cancel = cancel
					go func(e *watchedFile) {
						err := m.read(ctx, e)
						cancel()
						select {
						case m.results <- watchResult{e, err}:
						case <-m.ctx.Done():
						}
					}(entry)
				}
			}
			if scan {
				lastScan = now
				syncDirectories()
			}
		}
	}
}
