package main

import (
	"embed"
	"io/fs"
	"net/http"
)

//go:embed web
var embedFS embed.FS

var webFS http.FileSystem

func init() {
	sub, err := fs.Sub(embedFS, "web")
	if err != nil {
		panic(err)
	}
	webFS = http.FS(sub)
}
