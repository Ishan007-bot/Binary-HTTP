# Annotated hexdump: one complete request and response

Captured from a real run, not written by hand:

```
$ ./bserve ./www 9000
$ ./bcurl -v localhost:9000/index.html
```

`www/index.html` is the 12-byte file `<h1>hi</h1>\n`. All numbers are
big-endian. Hex values are shown in lower case, exactly as `bcurl -v` printed
them. Section numbers such as "SPEC 2" refer to [SPEC.md](SPEC.md).

In total, the client sent **67 bytes**: a 4-byte preface and one 63-byte frame.
The server sent back **96 bytes**: a 76-byte HEADERS frame and a 20-byte DATA
frame. The response body was 12 of those bytes.

---

## 1. Client → server: preface (4 bytes, SPEC 1)

```
42 48 54 01
```

| Bytes | Value | Meaning |
|---|---|---|
| `42 48 54` | "BHT" | Says "this is BHT/1, not HTTP/1.1". A plain HTTP client would send `47 45 54 20` ("GET ") and be disconnected. |
| `01` | 1 | Protocol version 1. It is sent once per connection, never per frame. |

## 2. Client → server: request HEADERS frame (8 + 55 bytes)

```
00 00 37 01 01 00 00 01  01 00 03 47 45 54 02 00
0b 2f 69 6e 64 65 78 2e  68 74 6d 6c 04 00 0e 6c
6f 63 61 6c 68 6f 73 74  3a 39 30 30 30 05 00 09
62 63 75 72 6c 2f 31 2e  30 06 00 03 2a 2f 2a
```

**Frame header (SPEC 2)**

| Bytes | Field | Value | Meaning |
|---|---|---|---|
| `00 00 37` | Length (24 bits) | 55 | 55 payload bytes follow this 8-byte header. |
| `01` | Type | 0x01 | HEADERS. |
| `01` | Flags | 0x01 | END_STREAM: the request is complete and has no body (SPEC 5). |
| `00 00 01` | Stream ID (24 bits) | 1 | The first request on this connection. The response must use the same ID. |

**Header block (SPEC 4).** Each field is a 1-byte name code, then a 2-byte
value length, then the value.

| Bytes | Code → name | Value length | Value |
|---|---|---|---|
| `01` `00 03` `47 45 54` | 0x01 → `:method` | 3 | `GET` |
| `02` `00 0b` `2f 69 6e 64 65 78 2e 68 74 6d 6c` | 0x02 → `:path` | 11 | `/index.html` |
| `04` `00 0e` `6c 6f 63 61 6c 68 6f 73 74 3a 39 30 30 30` | 0x04 → `host` | 14 | `localhost:9000` |
| `05` `00 09` `62 63 75 72 6c 2f 31 2e 30` | 0x05 → `user-agent` | 9 | `bcurl/1.0` |
| `06` `00 03` `2a 2f 2a` | 0x06 → `accept` | 3 | `*/*` |

Check: 6 + 14 + 17 + 12 + 6 = **55**, which matches the Length field. Every
name is in the static table, so none is spelled out. A name outside the table
would start with code `00` followed by the name with its own length.

These bytes are identical to the test vector in SPEC 7.

## 3. Server → client: response HEADERS frame (8 + 68 bytes)

```
00 00 44 01 00 00 00 01  03 00 03 32 30 30 07 00
09 74 65 78 74 2f 68 74  6d 6c 08 00 02 31 32 09
00 0a 62 73 65 72 76 65  2f 31 2e 30 0a 00 1d 54
75 65 2c 20 30 36 20 4f  63 74 20 32 30 32 36 20
31 32 3a 32 33 3a 31 30  20 47 4d 54
```

**Frame header**

| Bytes | Field | Value | Meaning |
|---|---|---|---|
| `00 00 44` | Length | 68 | 68 payload bytes follow. |
| `01` | Type | 0x01 | HEADERS. |
| `00` | Flags | 0x00 | No END_STREAM, so a body follows in DATA frames. |
| `00 00 01` | Stream ID | 1 | Answers request 1. |

**Header block**

| Bytes | Code → name | Value length | Value |
|---|---|---|---|
| `03` `00 03` `32 30 30` | 0x03 → `:status` | 3 | `200` (three ASCII digits, SPEC 5) |
| `07` `00 09` `74 65 78 74 2f 68 74 6d 6c` | 0x07 → `content-type` | 9 | `text/html` |
| `08` `00 02` `31 32` | 0x08 → `content-length` | 2 | `12` |
| `09` `00 0a` `62 73 65 72 76 65 2f 31 2e 30` | 0x09 → `server` | 10 | `bserve/1.0` |
| `0a` `00 1d` `54 75 65 … 47 4d 54` | 0x0A → `date` | 29 | `Tue, 06 Oct 2026 12:23:10 GMT` |

Check: 6 + 12 + 5 + 13 + 32 = **68**, which matches the Length field.

## 4. Server → client: DATA frame (8 + 12 bytes)

```
00 00 0c 00 01 00 00 01  3c 68 31 3e 68 69 3c 2f
68 31 3e 0a
```

| Bytes | Field | Value | Meaning |
|---|---|---|---|
| `00 00 0c` | Length | 12 | 12 body bytes follow. |
| `00` | Type | 0x00 | DATA. |
| `01` | Flags | 0x01 | END_STREAM: this is the last frame of response 1. |
| `00 00 01` | Stream ID | 1 | Belongs to request 1. |
| `3c 68 31 3e 68 69 3c 2f 68 31 3e 0a` | Payload | | `<h1>hi</h1>\n`. The body is 12 bytes, as `content-length` said. `bcurl` writes these bytes, and only these, to stdout. |

---

## What happens next on the connection

The connection stays open (SPEC 1).

- A second request would arrive as another HEADERS frame on stream 2.
- A frame of a type the receiver does not know, such as `00 00 05 7f 00 00 00 00`
  followed by 5 bytes, would be read using its Length field and thrown away
  (SPEC 3).
- To fetch several files over this one connection:
  `./bcurl localhost:9000/index.html localhost:9000/sub/`
