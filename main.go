package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
)

func pack(paths []string, output string, size int, dedupe bool) (Manifest, error) {
	if len(paths) == 0 || output == "" || size < 3 || size > 4096 {
		return Manifest{}, errors.New("provide PNG inputs, -out FILE.zip and a page size from 3 to 4096")
	}
	dest, err := os.Stat(output)
	if err != nil && !os.IsNotExist(err) {
		return Manifest{}, err
	}
	seen := map[string]bool{}
	sprites := make([]sprite, 0, len(paths))
	remaining := maxPixels
	for _, path := range paths {
		if dest != nil {
			info, err := os.Stat(path)
			if err != nil {
				return Manifest{}, err
			}
			if os.SameFile(info, dest) {
				return Manifest{}, errors.New("output aliases an input")
			}
		}
		name := filepath.Base(path)
		if seen[name] {
			return Manifest{}, fmt.Errorf("duplicate frame name %q", name)
		}
		seen[name] = true
		s, err := readSprite(path, size, remaining)
		if err != nil {
			return Manifest{}, fmt.Errorf("%s: %w", path, err)
		}
		remaining -= s.frame.Atlas[2] * s.frame.Atlas[3]
		sprites = append(sprites, s)
	}
	temp, err := os.CreateTemp(filepath.Dir(output), ".sprite-crate-*.zip")
	if err != nil {
		return Manifest{}, err
	}
	defer os.Remove(temp.Name())
	m, err := build(temp, sprites, size, dedupe)
	closeErr := temp.Close()
	if err != nil {
		return Manifest{}, err
	}
	if closeErr != nil {
		return Manifest{}, closeErr
	}
	return m, os.Rename(temp.Name(), output)
}

func main() {
	size := flag.Int("size", 256, "width and height of each atlas page")
	dedupe := flag.Bool("dedupe", false, "share one region for identical normalized crops")
	output := flag.String("out", "", "destination ZIP containing PNG pages and a JSON manifest")
	flag.Parse()
	m, err := pack(flag.Args(), *output, *size, *dedupe)
	if err == nil {
		err = json.NewEncoder(os.Stdout).Encode(m)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
