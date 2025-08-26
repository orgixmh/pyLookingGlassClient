#!/usr/bin/env python3
import os, mmap, struct

class LGMPv6:
    """
    Minimal LGMP v6 reader for a *fixed absolute* slot 0 buffer.
    Uses idx_off to compute current producer slot if you want, but with --offset we just read --slot.
    No JSON configs; everything is passed in or defaulted.
    """
    def __init__(self, shm, width=1920, height=1080, pitch=5888, bpp=3,
                 idx_off=0x10, force_offset=None, force_slot=0, nbuf=2):
        self.fd  = os.open(shm, os.O_RDWR)
        st       = os.fstat(self.fd)
        self.size= st.st_size
        self.mm  = mmap.mmap(self.fd, self.size, mmap.MAP_SHARED, mmap.PROT_READ|mmap.PROT_WRITE)

        if self.mm[:4] != b"LGMP":
            raise RuntimeError("Not an LGMP segment")

        self.fb_w   = int(width)
        self.fb_h   = int(height)
        self.pitch  = int(pitch)
        self.bpp    = int(bpp)
        self.idx_off= int(idx_off)
        self.nbuf   = int(nbuf)

        self.force_offset = force_offset
        self.force_slot   = int(force_slot)

    def close(self):
        try: self.mm.close()
        except: pass
        os.close(self.fd)

    def current_slot(self):
        # If forcing an absolute offset, respect --slot and ignore the header index.
        if self.force_offset is not None:
            return self.force_slot
        idx = struct.unpack_from("<I", self.mm, self.idx_off)[0]
        return idx % self.nbuf

    def slot_offset(self, slot):
        if self.force_offset is not None:
            # frames are not guaranteed tight in LGMP; but for your working setup
            # we only ever read slot 0 (default). If you change --slot, we assume tight layout.
            fsz = self.pitch * self.fb_h
            return self.force_offset + slot * fsz
        # fallback heuristic if you *don’t* force an offset
        base = 4096
        fsz  = self.pitch * self.fb_h
        return base + slot * fsz

    def read_frame_tight(self, slot):
        off = self.slot_offset(slot)
        fsz = self.pitch * self.fb_h
        if off < 0 or off + fsz > self.size:
            return None
        if self.pitch == self.fb_w * self.bpp:
            return self.mm[off: off + fsz]
        # repack pitched -> tight
        tight = self.fb_w * self.bpp
        out = bytearray(self.fb_w * self.fb_h * self.bpp)
        src = off; dst = 0
        for _ in range(self.fb_h):
            out[dst:dst+tight] = self.mm[src:src+tight]
            src += self.pitch
            dst += tight
        return bytes(out)
