package main

// #include <stdlib.h>
import "C"
import "unsafe"

// freeC releases memory allocated by C (malgo's DeviceID.Pointer uses C.CBytes).
func freeC(p unsafe.Pointer) { C.free(p) }
