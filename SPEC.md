# BHT/1 — HTTP semantics in binary frames

The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119. All
integers are unsigned and **big-endian** (network order). "u16" is a 2-byte
integer and "u24" is a 3-byte integer.

## 1. Connection

BHT/1 runs over one TCP connection. The client opens it and then sends a
4-byte **preface** before anything else:

```
42 48 54 01        "BHT" followed by version 0x01
```

If the first 4 bytes do not match, the server MUST close the connection
without replying. The preface lets a server reject a stray HTTP/1.1 client
(whose first bytes are `GET `) at once. It also states the version, so
individual frames do not need to carry one.

After the preface, both sides exchange **frames**. The connection is
persistent: it carries any number of requests. The server MUST keep it open
after every response, including errors. Only the client closes it, by
closing its socket. A server MAY close a connection that has been idle for
60 s or more. A client MUST NOT open a second connection to send further
requests to the same server.

## 2. Frame header

Every frame starts with a fixed **8-byte** header. A payload of exactly
`Length` bytes follows it.

```
byte:   0       1       2       3       4       5       6       7
     +-------+-------+-------+-------+-------+-------+-------+-------+
     |      Length (u24)     | Type  | Flags |    Stream ID (u24)    |
     +-------+-------+-------+-------+-------+-------+-------+-------+
```

| Field | Bits | Why this width |
|---|---|---|
| Length | 24 | The payload is at most 16 MiB − 1. That caps what a receiver must buffer for one frame. Bodies larger than that are split across several DATA frames. A 32-bit length would invite 4 GiB allocations. |
| Type | 8 | 256 frame types. Only two are defined. The rest are what a version 2 grows into (section 3). |
| Flags | 8 | Yes/no markers whose meaning depends on the frame type. Only one is defined. |
| Stream ID | 24 | Matches a response to its request. The client numbers requests 1, 2, 3 and so on, so 16 M requests per connection is ample. 0 is reserved for messages about the whole connection. |

**Compared with HTTP/2 (24/8/8/31, 9 bytes).** HTTP/2 needs 31-bit stream IDs.
They are never reused, and both peers open streams: clients use odd numbers
and servers even ones for push. BHT/1 has no push and only one stream at a
time per request, so 24 bits is enough. The header then comes to exactly
8 bytes, a single 64-bit word.

## 3. Frame types and the extension rule

| Type | Name | Flags | Payload |
|---|---|---|---|
| `0x00` | DATA | `0x01` END_STREAM | Body bytes, opaque. |
| `0x01` | HEADERS | `0x01` END_STREAM | A header block (section 4). |
| `0x02`–`0xFF` | reserved | — | — |

**A receiver that meets a frame type it does not know MUST read and discard
exactly `Length` payload bytes and continue as if the frame were absent.**
Every frame states its length in the same place, whatever its type, so any
receiver can skip any frame. That is what leaves room for a version 2.

In the same spirit:

- Senders MUST set undefined flag bits to 0. Receivers MUST ignore them.
- Every value of `Length` is legal, so a receiver never loses its place in
  the byte stream.

END_STREAM marks the last frame of a message.

## 4. Header block

A HEADERS payload is a sequence of fields that fills the payload exactly.
Each field starts with a 1-byte **name code**:

```
code 0x01–0x0A  indexed name:  [code][u16 value-len][value]
code 0x00       literal name:  [0x00][u16 name-len][name][u16 value-len][value]
code 0x0B–0xFF  reserved:      the block is malformed
```

**Static table.** It holds exactly the ten names the reference programs send.

| Code | Name | Code | Name |
|---|---|---|---|
| `0x01` | `:method` | `0x06` | `accept` |
| `0x02` | `:path` | `0x07` | `content-type` |
| `0x03` | `:status` | `0x08` | `content-length` |
| `0x04` | `host` | `0x09` | `server` |
| `0x05` | `user-agent` | `0x0A` | `date` |

Rules for names and values:

- A literal name MUST be non-empty, printable ASCII (`0x21`–`0x7E`), with no
  uppercase letters.
- A sender SHOULD use the code whenever the name is in the table.
- Values are opaque bytes and MAY be empty. Numbers such as `:status` and
  `content-length` are written as ASCII decimal digits.

The design is HPACK's first two mechanisms: a static table of indexed names,
and length-prefixed literals for everything else. It leaves out Huffman coding
and the dynamic table. Lengths are fixed u16 rather than HPACK's
variable-length integers, so two strangers encode them identically on the
first try. That limits each name or value to 64 KiB − 1 bytes.

## 5. Request and response

**Request.** A request is one HEADERS frame with END_STREAM set. Version 1
requests have no body.

- The block MUST contain `:method` and `:path`, each exactly once.
- The client SHOULD also send `host`, `user-agent` and `accept`.
- Stream IDs MUST strictly increase on a connection.
- A server MUST discard any DATA frames a client sends.

**Response.** A response is one HEADERS frame on the same stream ID as the
request.

- The block MUST contain `:status` as three ASCII digits. The server SHOULD
  also send `content-type`, `content-length`, `server` and `date`.
- If the body is empty, END_STREAM is set on the HEADERS frame. Otherwise one
  or more DATA frames follow, and the last one carries END_STREAM.
- The server answers requests one at a time, in the order received. A client
  MAY pipeline requests, sending several before it reads any response.

**Methods.** A server MUST support `GET`. It MAY support `HEAD`: the same
headers as `GET`, with END_STREAM set on HEADERS and no DATA.

**Path mapping.** `:path` MUST begin with `/`. The server:

1. strips any `?query` or `#fragment`;
2. percent-decodes the rest;
3. treats a path that names a directory as `<directory>/index.html`.

The server MUST NOT serve anything outside its root. A path is refused with
404 if it contains any of the following:

- a `..` or `.` segment, before or after decoding;
- a NUL byte, `\` or `:`;
- a link that resolves outside the root.

## 6. Errors

A frame whose framing is sound but whose contents break these rules affects
only its own request. **The connection stays open.**

| Status | When |
|---|---|
| 400 | Any of these: the block is truncated or has trailing garbage; a reserved name code; a bad literal name; a missing or repeated pseudo-header (`:method` or `:path`); a HEADERS frame without END_STREAM; a stream ID of 0 or one not above the previous. |
| 404 | The file does not exist, is not a regular file, or the path was refused (section 5). |
| 405 | The method is neither `GET` nor `HEAD`. |
| 500 | The file exists but could not be read. |

An error response has the same form as any other response. It SHOULD carry a
short `text/plain` body. The only error that closes the connection is a bad
preface.

## 7. Worked example (test vector)

The client requests `/index.html`. The server returns a 12-byte body.

```
C→S  42 48 54 01                      preface "BHT" v1
C→S  00 00 37 01 01 00 00 01          HEADERS len=55 flags=END_STREAM stream=1
     01 00 03 47 45 54                :method  "GET"
     02 00 0B 2F 69 6E 64 65 78 2E 68 74 6D 6C          :path "/index.html"
     04 00 0E 6C 6F 63 61 6C 68 6F 73 74 3A 39 30 30 30 host "localhost:9000"
     05 00 09 62 63 75 72 6C 2F 31 2E 30                user-agent "bcurl/1.0"
     06 00 03 2A 2F 2A                                  accept "*/*"

S→C  00 00 44 01 00 00 00 01          HEADERS len=68 flags=0 stream=1
     03 00 03 32 30 30                :status "200"
     07 00 09 74 65 78 74 2F 68 74 6D 6C                content-type "text/html"
     08 00 02 31 32                                     content-length "12"
     09 00 0A 62 73 65 72 76 65 2F 31 2E 30             server "bserve/1.0"
     0A 00 1D 54 75 65 2C 20 30 36 20 4F 63 74 20 32 30 32 36 20
              31 32 3A 30 30 3A 30 30 20 47 4D 54       date "Tue, 06 Oct 2026 12:00:00 GMT"
S→C  00 00 0C 00 01 00 00 01          DATA len=12 flags=END_STREAM stream=1
     3C 68 31 3E 68 69 3C 2F 68 31 3E 0A                "<h1>hi</h1>\n"
```
