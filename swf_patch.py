"""Rewrite the game SWF's BASE_PATH so Ruffle can open the AMF connection.

The SWF stores BASE_PATH as the bare host "127.0.0.1" and builds request URLs by
concatenating it with a path, e.g. "127.0.0.1" + "flashservices/gateway.php".
Flash Player resolved that against the SWF's own URL, so it worked.

Ruffle does not. Its AVM2 NetConnection.connect() only sets up Flash Remoting
when the URL starts with "http://" or "https://" -- anything else reaches an
unimplemented stub, so no connection handle is created and the game's first
call() throws Error #2126 ("NetConnection object must be connected"). The game
then loads its UI but never reaches the server, so no save data arrives.

Rewriting BASE_PATH to an absolute "http://host:port/" fixes it for Ruffle and
stays correct under Flash Player. The constant lives in the ABC constant pool as
a length-prefixed string, so growing it means fixing the enclosing DoABC tag
length and the SWF header's file length as well.

This runs on the bytes being served; the asset file on disk is never modified.
"""

import struct
import zlib

# The ABC constant pool stores strings as a u30 length followed by UTF-8 bytes.
# 0x09 is the length of "127.0.0.1".
BASE_PATH_ENTRY = b"\x09127.0.0.1"


class SwfPatchError(Exception):
    pass


def _rect_byte_len(body):
    nbits = body[0] >> 3
    return (5 + 4 * nbits + 7) // 8


def _iter_tags(body):
    """Yield (header_start, is_long, data_start, data_len) for each SWF tag."""
    pos = _rect_byte_len(body) + 4  # RECT, then FrameRate (u16) and FrameCount (u16)
    while pos + 2 <= len(body):
        header_start = pos
        (tag_code_and_length,) = struct.unpack_from("<H", body, pos)
        pos += 2
        code = tag_code_and_length >> 6
        length = tag_code_and_length & 0x3F
        is_long = length == 0x3F
        if is_long:
            (length,) = struct.unpack_from("<I", body, pos)
            pos += 4
        yield header_start, is_long, pos, length
        pos += length
        if code == 0:  # End tag
            return


def patch_base_path(swf_bytes, base_url):
    """Return swf_bytes with BASE_PATH replaced by base_url.

    base_url should be absolute and end with "/", e.g. "http://127.0.0.1:5005/".
    Raises SwfPatchError if the SWF does not look the way we expect, so the
    caller can fall back to serving it unmodified.
    """
    if not base_url.startswith(("http://", "https://")):
        raise SwfPatchError("base_url must start with http:// or https://")

    new_value = base_url.encode()
    if len(new_value) > 0x7F:
        # A longer string would need a multi-byte u30 length prefix.
        raise SwfPatchError("base_url is too long to encode in one length byte")
    new_entry = bytes([len(new_value)]) + new_value

    if swf_bytes[:3] == b"CWS":
        body = zlib.decompress(swf_bytes[8:])
        compressed = True
    elif swf_bytes[:3] == b"FWS":
        body = swf_bytes[8:]
        compressed = False
    else:
        raise SwfPatchError(f"unsupported SWF signature {swf_bytes[:3]!r}")

    found = body.find(BASE_PATH_ENTRY)
    if found == -1:
        raise SwfPatchError("BASE_PATH string not found")
    if body.find(BASE_PATH_ENTRY, found + 1) != -1:
        raise SwfPatchError("BASE_PATH string is ambiguous")

    delta = len(new_entry) - len(BASE_PATH_ENTRY)

    enclosing = None
    for header_start, is_long, data_start, data_len in _iter_tags(body):
        if data_start <= found < data_start + data_len:
            enclosing = (header_start, is_long, data_len)
            break
    if enclosing is None:
        raise SwfPatchError("no tag contains the BASE_PATH string")
    header_start, is_long, data_len = enclosing
    if not is_long:
        # A short tag stores its length in 6 bits, which cannot absorb a change.
        raise SwfPatchError("BASE_PATH sits in a short tag")

    patched = bytearray(body)
    patched[found:found + len(BASE_PATH_ENTRY)] = new_entry
    struct.pack_into("<I", patched, header_start + 2, data_len + delta)

    # Show in-game Wood, Oil, Ore/Iron, and Immunity HUD bars immediately instead
    # of hiding them behind early quests / level > 7 (PlayerStatsQuestGatedUIItem.isHidden).
    hud_gate_old = bytes.fromhex("d03060b70f669a5666b516762a111000002960b70f669a56d0669d534686180176822a110e00002960b70f669a5666d7162407af829648")
    pos_hud = patched.find(hud_gate_old)
    if pos_hud != -1:
        # Replace body after getlocal_0; pushscope (d0 30) with pushfalse; returnvalue (27 48).
        patched[pos_hud + 2:pos_hud + 4] = b"\x27\x48"

    (file_length,) = struct.unpack_from("<I", swf_bytes, 4)
    header = bytearray(swf_bytes[:8])
    struct.pack_into("<I", header, 4, file_length + delta)

    if compressed:
        return bytes(header) + zlib.compress(bytes(patched), 6)
    return bytes(header) + bytes(patched)
