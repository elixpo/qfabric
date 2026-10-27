#include <Arduino_RouterBridge.h>
#include <zephyr/kernel.h>

#include <cstdint>

#include "qtasks/add.qtask.h"
#include "qtasks/kernels.qtask.h"

// Stage 12 RT executor. A request is "TASK PASSES HEX_PAYLOAD"; the response
// is "RESULT LOCAL_EXECUTION_NS", or "error REASON". One RPC per invocation
// keeps diagnostics off the measured path.

qf_block_t qf_block{};

int qf_hex_value(char digit) {
  if (digit >= '0' && digit <= '9') return digit - '0';
  if (digit >= 'a' && digit <= 'f') return digit - 'a' + 10;
  return -1;
}

bool qf_decode_block(const String &hex) {
  if (hex.length() != 2 * QF_BLOCK_BYTES) return false;
  for (unsigned int i = 0; i < QF_BLOCK_BYTES; ++i) {
    const int high = qf_hex_value(hex.charAt(2 * i));
    const int low = qf_hex_value(hex.charAt(2 * i + 1));
    if (high < 0 || low < 0) return false;
    qf_block.bytes[i] = static_cast<std::uint8_t>((high << 4) | low);
  }
  return true;
}

String qf_stage12_invoke(String request) {
  const int first = request.indexOf(' ');
  const int second = first < 0 ? -1 : request.indexOf(' ', first + 1);
  if (first < 0 || second < 0) return String("error invalid-request");
  const String task = request.substring(0, first);
  const long passes = request.substring(first + 1, second).toInt();
  if (passes < 1 || !qf_decode_block(request.substring(second + 1))) {
    return String("error invalid-request");
  }

  std::int64_t result = 0;
  const std::uint32_t started = k_cycle_get_32();
  if (task == "add") {
    result = qf_task_add(2, 3);
  } else if (task == "crc32") {
    result = qf_task_crc32(qf_block);
  } else if (task == "fir") {
    result = qf_task_fir(qf_block, static_cast<std::uint32_t>(passes));
  } else {
    return String("error unknown-task");
  }
  const std::uint32_t elapsed = k_cycle_get_32() - started;
  const std::uint64_t local_ns = k_cyc_to_ns_floor64(elapsed);
  // Results are reported modulo 2^32; the Linux profiler compares likewise.
  return String(static_cast<unsigned long>(static_cast<std::uint32_t>(result))) + " " +
         String(static_cast<unsigned long>(local_ns));
}

void setup() {
  if (!Bridge.begin()) {
    while (true) delay(1000);
  }
  Bridge.provide("qf_stage12_invoke", qf_stage12_invoke);
}

void loop() {
  delay(1);
}
