package main

import (
	"encoding/binary"
	"runtime"
	"syscall"
	"unsafe"
)

// Linux birth/ctime via the raw statx syscall (stdlib only: syscall +
// unsafe, no cgo). struct statx has a fixed 256-byte layout; anything
// else means our offsets are wrong for this platform -> disabled.
const (
	statxBTIME = 0x800
	statxNoFollow = 0x100
	// struct statx field offsets (bytes)
	offMask  = 0
	offBtime = 80
	offCtime = 96
	statxSize = 256
)

var statxOK = runtime.GOOS == "linux" &&
	(runtime.GOARCH == "amd64" || runtime.GOARCH == "arm64")

const sysStatx = 332 // __NR_statx on asm-generic (amd64/arm64)

func statxNumber() uintptr {
	return sysStatx
}

// StatxTimes returns (ctime, btime) epoch seconds; each is nil when
// unavailable. Never panics: any failure yields nils.
func StatxTimes(path string) (ctime, btime *float64) {
	if !statxOK {
		return nil, nil
	}
	p, err := syscall.BytePtrFromString(path)
	if err != nil {
		return nil, nil
	}
	var buf [statxSize]byte
	atFDCWD := -100
	// #nosec G103 -- raw syscall with a fixed-size buffer, bounds known.
	r1, _, errno := syscall.Syscall6(statxNumber(),
		uintptr(atFDCWD), uintptr(unsafe.Pointer(p)),
		uintptr(statxNoFollow), uintptr(statxBTIME),
		uintptr(unsafe.Pointer(&buf[0])), 0)
	if r1 != 0 || errno != 0 {
		return nil, nil
	}
	mask := binary.LittleEndian.Uint32(buf[offMask:])
	ct := tsToFloat(buf[offCtime : offCtime+16 : offCtime+16])
	ctime = &ct
	if mask&statxBTIME != 0 {
		bt := tsToFloat(buf[offBtime : offBtime+16 : offBtime+16])
		btime = &bt
	}
	return ctime, btime
}

func tsToFloat(ts []byte) float64 {
	sec := int64(binary.LittleEndian.Uint64(ts[0:8]))
	nsec := binary.LittleEndian.Uint32(ts[8:12])
	return float64(sec) + float64(nsec)/1e9
}
