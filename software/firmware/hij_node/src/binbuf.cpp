#include "binbuf.h"

#include <LittleFS.h>

#include "clock.h"
#include "config.h"

#define BUF_MAGIC 0x48494A42UL  // "HIJB"
#define BUF_VERSION 1
#define BUF_FILE "/bins.bin"
#define BUF_TMP "/bins.tmp"

namespace {

struct Segment {
  uint64_t tStartMs;  // inicio del primer bin del segmento
  uint32_t session;   // base de tiempos (clockSession) con la que se midió tStartMs
  uint16_t firstSeq;
  uint16_t count;
  uint16_t deltaS;
  uint8_t epoch;
  uint8_t reserved;
};

struct Header {
  uint32_t magic;
  uint16_t version;
  uint16_t count;
  uint16_t sent;
  uint16_t acked;
  uint16_t sentHigh;
  uint8_t nSeg;
  uint8_t overflow;
  Segment seg[MAX_SEGMENTS];
};

Header h;
uint16_t steps[MAX_BUF_BINS];  // steps[0] es el bin más antiguo
uint16_t cap = DEF_BUF_BINS;
bool dirty = false;
bool fsOk = false;

uint32_t crcUpdate(uint32_t c, const uint8_t* d, size_t n) {
  for (size_t i = 0; i < n; i++) {
    c ^= d[i];
    for (int b = 0; b < 8; b++) c = (c >> 1) ^ (0xEDB88320UL & (0UL - (c & 1)));
  }
  return c;
}

uint32_t stateCrc() {
  const uint32_t c = crcUpdate(0xFFFFFFFFUL, reinterpret_cast<const uint8_t*>(&h), sizeof(h));
  return crcUpdate(c, reinterpret_cast<const uint8_t*>(steps), h.count * sizeof(uint16_t));
}

// Ya no hace falta conservar el bin: entregado (S0 a S2) o confirmado (S3)
bool frontDone(uint16_t m, uint8_t strategy) { return strategy >= 3 ? h.acked >= m : h.sent >= m; }

void dropFront(uint16_t m) {
  if (m > h.count) m = h.count;
  if (m == 0) return;
  memmove(steps, steps + m, (h.count - m) * sizeof(uint16_t));
  h.count -= m;
  h.sent = h.sent > m ? h.sent - m : 0;
  h.acked = h.acked > m ? h.acked - m : 0;
  h.sentHigh = h.sentHigh > m ? h.sentHigh - m : 0;
  while (m && h.nSeg) {
    Segment& s = h.seg[0];
    const uint16_t take = m < s.count ? m : s.count;
    s.count -= take;
    s.firstSeq += take;
    s.tStartMs += (uint64_t)take * s.deltaS * 1000ULL;
    m -= take;
    if (s.count == 0) {
      memmove(&h.seg[0], &h.seg[1], (h.nSeg - 1) * sizeof(Segment));
      h.nSeg--;
    }
  }
  dirty = true;
}

bool load() {
  File f = LittleFS.open(BUF_FILE, "r");
  if (!f) return false;
  bool ok = f.read(reinterpret_cast<uint8_t*>(&h), sizeof(h)) == sizeof(h) && h.magic == BUF_MAGIC &&
            h.version == BUF_VERSION && h.count <= MAX_BUF_BINS && h.nSeg <= MAX_SEGMENTS;
  uint32_t crc = 0;
  ok = ok && f.read(reinterpret_cast<uint8_t*>(steps), h.count * sizeof(uint16_t)) == h.count * sizeof(uint16_t);
  ok = ok && f.read(reinterpret_cast<uint8_t*>(&crc), sizeof(crc)) == sizeof(crc) && crc == stateCrc();
  f.close();
  return ok;
}

}  // namespace

void bufBegin(uint16_t capacity) {
  cap = capacity > MAX_BUF_BINS ? MAX_BUF_BINS : capacity;
  fsOk = LittleFS.begin(true);
  if (!fsOk || !load()) memset(&h, 0, sizeof(h));
  if (h.count > cap) {
    // se ha reducido B: se descartan los más antiguos
    if (!frontDone(h.count - cap, 3)) h.overflow = 1;
    dropFront(h.count - cap);
  }
}

PushResult bufPush(uint8_t epoch, uint16_t seq, uint16_t deltaS, uint64_t tStartMs, uint16_t value, uint8_t strategy) {
  PushResult r = {false, 0, 0};
  auto drop = [&](uint16_t m) {
    if (!frontDone(m, strategy) && !r.lost) {
      const BinView b = bufAt(0);
      r = {true, b.epoch, b.seq};
      h.overflow = 1;
    }
    dropFront(m);
  };
  if (h.count >= cap) drop(1);

  const Segment* last = h.nSeg ? &h.seg[h.nSeg - 1] : nullptr;
  const bool contiguous = last && last->epoch == epoch && last->session == clockSession() &&
                          last->deltaS == deltaS && (uint16_t)(last->firstSeq + last->count) == seq;
  if (!contiguous) {
    if (h.nSeg == MAX_SEGMENTS) drop(h.seg[0].count);  // demasiadas épocas: fuera la más antigua
    h.seg[h.nSeg++] = {tStartMs, clockSession(), seq, 0, deltaS, epoch, 0};
  }
  steps[h.count++] = value;
  h.seg[h.nSeg - 1].count++;
  dirty = true;
  return r;
}

uint16_t bufCount() { return h.count; }
uint16_t bufSent() { return h.sent; }
uint16_t bufAcked() { return h.acked; }
uint16_t bufSentHigh() { return h.sentHigh; }

BinView bufAt(uint16_t idx) {
  uint16_t base = 0;
  for (uint8_t i = 0; i < h.nSeg; i++) {
    const Segment& s = h.seg[i];
    if (idx < base + s.count) {
      const uint16_t off = idx - base;
      return {s.epoch,
              (uint16_t)(s.firstSeq + off),
              steps[idx],
              s.deltaS,
              s.tStartMs + (uint64_t)off * s.deltaS * 1000ULL,
              s.session == clockSession()};
    }
    base += s.count;
  }
  return {0, 0, 0, 0, 0, false};
}

uint16_t bufSegmentStart(uint16_t idx) {
  uint16_t base = 0;
  for (uint8_t i = 0; i < h.nSeg; i++) {
    if (idx < base + h.seg[i].count) return base;
    base += h.seg[i].count;
  }
  return base;
}

uint16_t bufSegmentEnd(uint16_t idx) {
  uint16_t base = 0;
  for (uint8_t i = 0; i < h.nSeg; i++) {
    base += h.seg[i].count;
    if (idx < base) return base;
  }
  return base;
}

void bufMarkSent(uint16_t end) {
  if (end > h.count) end = h.count;
  if (end > h.sent) h.sent = end;
  if (h.sent > h.sentHigh) h.sentHigh = h.sent;
  dirty = true;
}

int bufAck(uint8_t epoch, uint16_t seq) {
  for (uint16_t i = 0; i < h.count; i++) {
    const BinView b = bufAt(i);
    if (b.epoch != epoch || b.seq != seq) continue;
    if (i + 1 > h.acked) h.acked = i + 1;
    if (h.acked > h.sent) h.sent = h.acked;
    if (h.sent > h.sentHigh) h.sentHigh = h.sent;
    dirty = true;
    return i;
  }
  return -1;
}

void bufAckUpTo(uint16_t end) {
  if (end > h.count) end = h.count;
  if (end > h.acked) h.acked = end;
  if (h.acked > h.sent) h.sent = h.acked;
  if (h.sent > h.sentHigh) h.sentHigh = h.sent;
  dirty = true;
}

uint16_t bufRewind() {
  const uint16_t m = h.sent - h.acked;
  h.sent = h.acked;
  dirty = true;
  return m;
}

void bufRelease(uint8_t strategy, uint8_t k) {
  uint16_t m;
  switch (strategy) {
    case 3:
    case 4: m = h.acked; break;                       // se conservan hasta el ACK
    case 2: m = h.sent > k ? h.sent - k : 0; break;   // se conservan los k últimos enviados
    default: m = h.sent; break;
  }
  dropFront(m);
}

bool bufOverflowPending() { return h.overflow; }

void bufClearOverflow() {
  if (!h.overflow) return;
  h.overflow = 0;
  dirty = true;
}

void bufClear() {
  memset(&h, 0, sizeof(h));
  dirty = true;
}

void bufSave() {
  if (!dirty || !fsOk) return;
  h.magic = BUF_MAGIC;
  h.version = BUF_VERSION;
  const uint32_t crc = stateCrc();
  File f = LittleFS.open(BUF_TMP, "w");
  if (!f) return;
  f.write(reinterpret_cast<const uint8_t*>(&h), sizeof(h));
  f.write(reinterpret_cast<const uint8_t*>(steps), h.count * sizeof(uint16_t));
  f.write(reinterpret_cast<const uint8_t*>(&crc), sizeof(crc));
  f.close();
  // el renombrado es atómico: si se va la alimentación queda el fichero anterior completo
  if (!LittleFS.rename(BUF_TMP, BUF_FILE)) {
    LittleFS.remove(BUF_FILE);
    LittleFS.rename(BUF_TMP, BUF_FILE);
  }
  dirty = false;
}
