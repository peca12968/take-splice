package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"io"
	"os"
	"path/filepath"
	"sort"
)

type Frame struct {
	Name       string `json:"name"`
	SourceSize [2]int `json:"source_size"`
	Offset     [2]int `json:"offset"`
	Page       int    `json:"page"`
	Atlas      [4]int `json:"atlas"`
}

type Manifest struct {
	Version  int      `json:"version"`
	PageSize int      `json:"page_size"`
	Gutter   int      `json:"gutter"`
	Pages    []string `json:"pages"`
	Frames   []Frame  `json:"frames"`
}

type sprite struct {
	frame Frame
	crop  *image.NRGBA
}

const maxPixels = 16_777_216

func rgba8(pixel color.Color) color.NRGBA {
	if c, ok := pixel.(color.NRGBA64); ok {
		return color.NRGBA{R: uint8(c.R >> 8), G: uint8(c.G >> 8), B: uint8(c.B >> 8), A: uint8(c.A >> 8)}
	}
	return color.NRGBAModel.Convert(pixel).(color.NRGBA)
}

func readSprite(path string, size int, remaining int) (sprite, error) {
	f, err := os.Open(path)
	if err != nil {
		return sprite{}, err
	}
	defer f.Close()
	cfg, err := png.DecodeConfig(f)
	if err != nil {
		return sprite{}, err
	}
	if cfg.Width <= 0 || cfg.Height <= 0 || cfg.Width > maxPixels/cfg.Height {
		return sprite{}, fmt.Errorf("source canvas exceeds %d pixels", maxPixels)
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return sprite{}, err
	}
	img, err := png.Decode(f)
	if err != nil {
		return sprite{}, err
	}
	fm := Frame{Name: filepath.Base(path), SourceSize: [2]int{cfg.Width, cfg.Height}, Page: -1}
	left, top, right, bottom := cfg.Width, cfg.Height, 0, 0
	for y := 0; y < cfg.Height; y++ {
		for x := 0; x < cfg.Width; x++ {
			_, _, _, alpha := img.At(x, y).RGBA()
			if alpha>>8 != 0 {
				left, top = min(x, left), min(y, top)
				right, bottom = max(x+1, right), max(y+1, bottom)
			}
		}
	}
	if right == 0 {
		return sprite{frame: fm}, nil
	}
	w, h := right-left, bottom-top
	if w+2 > size || h+2 > size {
		return sprite{}, fmt.Errorf("%dx%d crop plus gutter exceeds page size %d", w, h, size)
	}
	if w*h > remaining {
		return sprite{}, fmt.Errorf("total cropped area exceeds %d pixels", maxPixels)
	}
	crop := image.NewNRGBA(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			c := rgba8(img.At(left+x, top+y))
			if c.A != 0 {
				crop.SetNRGBA(x, y, c)
			}
		}
	}
	fm.Offset = [2]int{left, top}
	fm.Atlas = [4]int{0, 0, w, h}
	return sprite{fm, crop}, nil
}

func drawWithGutter(page *image.NRGBA, crop *image.NRGBA, left, top int) {
	w, h := crop.Bounds().Dx(), crop.Bounds().Dy()
	for y := -1; y <= h; y++ {
		for x := -1; x <= w; x++ {
			c := crop.NRGBAAt(max(0, min(x, w-1)), max(0, min(y, h-1)))
			page.SetNRGBA(left+x, top+y, c)
		}
	}
}

// Record the first matching tile in sorted packing order. A digest narrows the
// search, but dimensions and RGBA bytes decide whether the tiles can share.
type tileIndex map[[sha256.Size]byte][]int

func (seen tileIndex) firstMatch(sprites []sprite, i int) int {
	crop := sprites[i].crop
	digest := sha256.Sum256(crop.Pix)
	for _, j := range seen[digest] {
		prior := sprites[j].crop
		if crop.Rect == prior.Rect && bytes.Equal(crop.Pix, prior.Pix) {
			return j
		}
	}
	seen[digest] = append(seen[digest], i)
	return i
}

func build(out io.Writer, sprites []sprite, size int, dedupe bool) (Manifest, error) {
	sort.Slice(sprites, func(i, j int) bool {
		a, b := sprites[i].frame, sprites[j].frame
		if a.Atlas[3] != b.Atlas[3] {
			return a.Atlas[3] > b.Atlas[3]
		}
		if a.Atlas[2] != b.Atlas[2] {
			return a.Atlas[2] > b.Atlas[2]
		}
		return a.Name < b.Name
	})
	m := Manifest{Version: 1, PageSize: size, Gutter: 1, Pages: []string{}, Frames: make([]Frame, len(sprites))}
	z := zip.NewWriter(out)
	var page *image.NRGBA
	flush := func() error {
		if page == nil {
			return nil
		}
		name := fmt.Sprintf("page-%03d.png", len(m.Pages))
		entry, err := z.CreateHeader(&zip.FileHeader{Name: name, Method: zip.Store})
		if err != nil {
			return err
		}
		if err = png.Encode(entry, page); err != nil {
			return err
		}
		m.Pages = append(m.Pages, name)
		return nil
	}
	x, y, rowHeight := 0, 0, 0
	seen := make(tileIndex)
	for i, s := range sprites {
		m.Frames[i] = s.frame
		fm := &m.Frames[i]
		if s.crop == nil {
			continue
		}
		if dedupe {
			if j := seen.firstMatch(sprites, i); j != i {
				fm.Page, fm.Atlas = m.Frames[j].Page, m.Frames[j].Atlas
				continue
			}
		}
		w, h := fm.Atlas[2]+2, fm.Atlas[3]+2
		if x+w > size {
			x, y, rowHeight = 0, y+rowHeight, 0
		}
		if y+h > size {
			if err := flush(); err != nil {
				return m, err
			}
			page = nil
			x, y, rowHeight = 0, 0, 0
		}
		if page == nil {
			page = image.NewNRGBA(image.Rect(0, 0, size, size))
		}
		fm.Page, fm.Atlas[0], fm.Atlas[1] = len(m.Pages), x+1, y+1
		drawWithGutter(page, s.crop, x+1, y+1)
		x += w
		rowHeight = max(rowHeight, h)
	}
	if err := flush(); err != nil {
		return m, err
	}
	sort.Slice(m.Frames, func(i, j int) bool { return m.Frames[i].Name < m.Frames[j].Name })
	entry, err := z.Create("manifest.json")
	if err != nil {
		return m, err
	}
	if err = json.NewEncoder(entry).Encode(m); err != nil {
		return m, err
	}
	return m, z.Close()
}
