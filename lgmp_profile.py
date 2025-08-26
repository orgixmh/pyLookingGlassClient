# constants pulled from boot
IDX_OFF_DEFAULT   = 0x10
FLAG_OFF_DEFAULT  = 0x13C
FLAG_MASK_DEFAULT = 0x00000004

# stable "set-bits" the official client turned on
SET_BITS = {
    0x28:  0x00000001,
    0x138: 0x436C6125,
    0x1C4: 0x00000001,
    0x4A8: 0x00000001,
    0x5B0: 0x436C6125,
    0x63C: 0x00000001,
    0x640: 0x00000001,
    0x648: 0x000101F4,
}

# fast ACK search windows + bounded fallback
ACK_RANGES_DEFAULT   = [(0x14, 0x200), (0x200, 0x400)]
ACK_FALLBACK_DEFAULT = (0x40, 0x20000)
