# BHT/1 — HTTP semantics in binary frames

**At a glance.** The client opens one TCP connection and sends `BHT\x01`.
After that, everything is a **frame**: an 8-byte header (length, type, flags,
stream ID) followed by `length` bytes. A request is one HEADERS frame. A
response is a HEADERS frame, then DATA frames, with the last frame marked
END_STREAM. Frame types a receiver does not know are skipped.

MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119. All integers are
unsigned and **big-endian**. "u16" is 2 bytes and "u24" is 3 bytes.

## 1. Connection

BHT/1 runs over one TCP connection. The client opens it and first sends a
4-byte **preface**: `42 48 54 01`, which is "BHT" and then version 1. If those
4 bytes do not match, the server MUST close the connection without replying.
This turns away a stray HTTP/1.1 client, whose first bytes are `GET `, and it
states the version once so that frames need not carry it.

After the preface, both sides send only frames. The connection is persistent:

- The server MUST keep it open after every response, including errors.
- The client closes it when it is done. The server MAY close it after 60 s
  idle.
- A client MUST NOT open a second connection to send further requests to the
  same server.

## 2. Frame header

Every frame is a fixed **8-byte** header followed by exactly `Length` payload
bytes.

```
byte:   0       1       2       3       4       5       6       7
     +-------+-------+-------+-------+-------+-------+-------+-------+
     |      Length (u24)     | Type  | Flags |    Stream ID (u24)    |
     +-------+-------+-------+-------+-------+-------+-------+-------+
```

| Field | Bits | Why this width |
|---|---|---|
| Length | 24 | Up to 16 MiB − 1 per frame, which caps what a receiver must buffer. Larger bodies span several DATA frames. 32 bits would invite 4 GiB allocations. |
| Type | 8 | 256 types. Two are defined; the rest are room for a version 2 (section 3). |
| Flags | 8 | Yes/no markers whose meaning depends on the type. One is defined. |
| Stream ID | 24 | Pairs a response with its request. The client counts 1, 2, 3…, so 16 M requests per connection is ample. 0 is reserved for the connection itself. |

**Compared with HTTP/2 (24/8/8/31, 9 bytes).** HTTP/2 needs 31-bit stream IDs
because IDs are never reused and both sides open streams: clients use odd IDs,
servers even ones for push. BHT/1 has no push, so 24 bits suffice, and the
header becomes exactly 8 bytes: one 64-bit word.

## 3. Frame types and the extension rule

| Type | Name | Flags | Payload |
|---|---|---|---|
| `0x00` | DATA | `0x01` END_STREAM | body bytes (opaque) |
| `0x01` | HEADERS | `0x01` END_STREAM | header block (section 4) |
| `0x02`–`0xFF` | reserved | | |

**A receiver that meets a frame type it does not know MUST read and discard
exactly `Length` payload bytes and carry on as if the frame were absent.**
Every frame states its length in the same place, whatever its type, so any
receiver can skip any frame. That is what leaves room for a version 2.

In the same spirit, senders MUST set undefined flag bits to 0, and receivers
MUST ignore them. Every `Length` value is legal, so a receiver never loses its
place in the byte stream. END_STREAM marks the last frame of a message.

## 4. Header block

A HEADERS payload is a sequence of fields that fills it exactly. Each field
starts with a 1-byte **name code**:

```
0x01–0x0A  indexed:   [code] [u16 value-len] [value]
0x00       literal:   [0x00] [u16 name-len] [name] [u16 value-len] [value]
0x0B–0xFF  reserved:  the block is malformed
```

**Static table:** exactly the ten names the reference programs send.

| Code | Name | Code | Name | Code | Name | Code | Name |
|---|---|---|---|---|---|---|---|
| `01` | `:method` | `04` | `host` | `07` | `content-type` | `0A` | `date` |
| `02` | `:path` | `05` | `user-agent` | `08` | `content-length` | | |
| `03` | `:status` | `06` | `accept` | `09` | `server` | | |

Rules for names and values:

- A literal name MUST be non-empty, printable ASCII (`0x21`–`0x7E`), with no
  uppercase letters.
- A sender SHOULD use the code whenever the name is in the table.
- Values are opaque bytes and MAY be empty. Numbers such as `:status` and
  `content-length` are written as ASCII decimal.

This is HPACK's first two mechanisms, a static table of names plus
length-prefixed literals, without Huffman coding or the dynamic table. Lengths
are fixed u16 rather than HPACK's variable-length integers, so two strangers
encode them identically on the first try. That limits a name or value to
64 KiB − 1 bytes.

## 5. Request and response

**Request.** One HEADERS frame with END_STREAM set; version 1 requests have
no body.

- It MUST contain `:method` and `:path`, each exactly once. It SHOULD also
  contain `host`, `user-agent` and `accept`.
- Stream IDs MUST strictly increase on a connection.
- The server MUST discard DATA frames from the client.

**Response.** A HEADERS frame on the request's stream ID.

- It MUST contain `:status`, as three ASCII digits. It SHOULD also contain
  `content-type`, `content-length`, `server` and `date`.
- If the body is empty, END_STREAM is set on the HEADERS frame. Otherwise one
  or more DATA frames follow, and the last carries END_STREAM.
- The server answers requests one at a time, in the order received. A client
  MAY pipeline, sending several requests before reading any response.

**Methods.** A server MUST support `GET`. It MAY support `HEAD`, which returns
the same headers with END_STREAM set on HEADERS and no DATA.

**Path mapping.** `:path` MUST start with `/`. The server:

1. strips any `?query` or `#fragment`;
2. percent-decodes the rest;
3. treats a path that names a directory as `<directory>/index.html`.

The server MUST NOT serve anything outside its root. It refuses a path with
404 if, before or after decoding, the path contains a `.` or `..` segment, a
NUL byte, `\` or `:`, or if it resolves through a link to a file outside the
root.

## 6. Errors

A frame with sound framing but invalid contents fails only its own request.
**The connection stays open.**

| Status | When |
|---|---|
| 400 | The block is truncated or has trailing bytes; it has a reserved name code or a bad literal name; `:method` or `:path` is missing or repeated; HEADERS arrives without END_STREAM; the stream ID is 0 or not above the previous one. |
| 404 | No such regular file, or the path was refused (section 5). |
| 405 | The method is neither `GET` nor `HEAD`. |
| 500 | The file exists but could not be read. |

An error response has the same form as any other response. It SHOULD carry a
short `text/plain` body. The only error that closes the connection is a bad
preface.

## 7. Worked example (test vector)

`GET /index.html`. The server returns a 12-byte body.

```
C→S  42 48 54 01                 preface "BHT" v1
C→S  00 00 37 01 01 00 00 01     HEADERS len=55 flags=END_STREAM stream=1
     01 00 03 47 45 54                                  :method "GET"
     02 00 0B 2F 69 6E 64 65 78 2E 68 74 6D 6C          :path "/index.html"
     04 00 0E 6C 6F 63 61 6C 68 6F 73 74 3A 39 30 30 30 host "localhost:9000"
     05 00 09 62 63 75 72 6C 2F 31 2E 30                user-agent "bcurl/1.0"
     06 00 03 2A 2F 2A                                  accept "*/*"
S→C  00 00 44 01 00 00 00 01     HEADERS len=68 flags=0 stream=1
     03 00 03 32 30 30                                  :status "200"
     07 00 09 74 65 78 74 2F 68 74 6D 6C                content-type "text/html"
     08 00 02 31 32                                     content-length "12"
     09 00 0A 62 73 65 72 76 65 2F 31 2E 30             server "bserve/1.0"
     0A 00 1D 54 75 65 2C 20 … 47 4D 54                 date (29 bytes) "Tue, 06 Oct 2026 12:00:00 GMT"
S→C  00 00 0C 00 01 00 00 01     DATA len=12 flags=END_STREAM stream=1
     3C 68 31 3E 68 69 3C 2F 68 31 3E 0A                "<h1>hi</h1>\n"
```
