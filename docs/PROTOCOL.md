# Protocol notes

What is known about the Aion 2 packets the overlay uses. All of this was found by recording traffic with `tools/aion2_capture.py` and comparing it with in-game actions. A game patch can change any of it.

Only **server → client** traffic is used. It is not encrypted. Client → server traffic is encrypted and not needed.

## Connection

- The game keeps one long-lived TCP connection to the game server, on a non-standard port.
- The overlay doesn't hard-code the address. It finds the TCP connections of `AION2.exe` with `psutil` and listens to every port they use except 80 and 443.
- If the PC uses a VPN, the plain traffic is visible on the VPN's virtual adapter, so the overlay listens on all adapters.

## Framing

The TCP stream is a sequence of frames:

```
varint  length      # protobuf-style LEB128
u16     opcode      # stored as two bytes; written below in stream order
...     payload
```

The whole frame, including the length varint, takes `length - 4 + size_of_varint` bytes.

### Compressed bundles

Opcode `ff ff` marks a compressed bundle:

```
varint  length
ff ff
u32le   uncompressed_size
...     LZ4 block (raw, no frame header)
```

Decompressed, a bundle is again a sequence of frames in the same format. The big bundles arrive when a character enters the world.

## Spirit point packets

### `00 90` — full state on entering the world

It sits inside an LZ4 bundle.

```
u32le   unknown
varint  n_levels
n_levels × { u32le pet_id, u32le pet_id (same), u32le level }
varint  n_points
n_points × { u32le pet_id, u32le points }
...     more data (not used)
```

- `level` is 0–3. `points` is the progress inside the current level and resets on every level-up.
- Pets that aren't listed are at 0.
- At level 3 the server always reports 0 points: it stops counting.

Example: `1097 → level 1, points 21` showed in game as 21/25 toward level 2.

IDs run from about 1001 to 1220 — one per pet, the same pets that can be ridden once unlocked (about 206 of them).

### `0d 90` — spirit points gained (one per kill)

```
u8      kind        # always 1 so far
u32le   pet_id
u32le   amount      # the "xN" in the chat line
```

This packet carries no total, so the client (and the overlay) adds `amount` to the last known state. Level-ups happen locally with thresholds 5 / 25 / 75.

## Pet names

Names aren't in the traffic. The overlay uses its own ID → name database, `data/names.json`, built with `tools/fetch_pet_names.py` from the MetaBot.GG database. For IDs missing there, it falls back to reading the chat. In the game client itself, names live in encrypted localization paks.

## Re-checking after a patch

1. Run `python tools/aion2_capture.py capture.jsonl` before entering the world.
2. Re-enter the world, then kill a few monsters and press F9 after each kill.
3. Look for frames that appear exactly once per F9 mark and for the state packet in the login burst. Search for a pet ID you know as `u32le`.
