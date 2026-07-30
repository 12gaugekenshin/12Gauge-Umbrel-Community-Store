import hashlib
import struct


def block_hash(block_hex):
    raw = bytes.fromhex(block_hex)
    if len(raw) < 81:
        raise ValueError("serialized block is too short")
    return hashlib.sha256(hashlib.sha256(raw[:80]).digest()).digest()[::-1].hex()


def compact_target(bits):
    exponent = bits >> 24
    coefficient = bits & 0x007FFFFF
    if bits & 0x00800000:
        raise ValueError("negative compact target")
    if exponent <= 3:
        return coefficient >> (8 * (3 - exponent))
    return coefficient << (8 * (exponent - 3))


def verify_proof(block_hex):
    raw = bytes.fromhex(block_hex)
    if len(raw) < 81:
        raise ValueError("serialized block is too short")
    bits = struct.unpack("<I", raw[72:76])[0]
    target = compact_target(bits)
    digest = hashlib.sha256(hashlib.sha256(raw[:80]).digest()).digest()
    hash_value = int.from_bytes(digest, "little")
    return {
        "hash": digest[::-1].hex(),
        "bits": f"{bits:08x}",
        "target": f"{target:064x}",
        "proofValid": 0 < target and hash_value <= target,
    }


def confirmation_status(confirmations):
    if confirmations == -1:
        return "orphaned"
    if confirmations is None or confirmations < 1:
        return "candidate"
    if confirmations < 6:
        return "confirming"
    if confirmations < 100:
        return "confirmed"
    return "mature"

