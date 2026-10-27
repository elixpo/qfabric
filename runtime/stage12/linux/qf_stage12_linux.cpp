#include <charconv>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <sstream>
#include <string>
#include <string_view>

#include "../../../qtasks/add.qtask.h"
#include "../../../qtasks/kernels.qtask.h"

// Persistent Stage 12 executor. Each stdin line is
//   INVOCATION_ID TASK PASSES HEX_PAYLOAD
// and produces one stdout line
//   INVOCATION_ID RESULT LOCAL_EXECUTION_NS
// so the profiler measures task dispatch rather than process creation.

namespace {

int hex_value(char digit) {
  if (digit >= '0' && digit <= '9') return digit - '0';
  if (digit >= 'a' && digit <= 'f') return digit - 'a' + 10;
  return -1;
}

bool decode_block(std::string_view hex, qf_block_t &block) {
  if (hex.size() != 2 * QF_BLOCK_BYTES) return false;
  for (std::size_t i = 0; i < QF_BLOCK_BYTES; ++i) {
    const int high = hex_value(hex[2 * i]);
    const int low = hex_value(hex[2 * i + 1]);
    if (high < 0 || low < 0) return false;
    block.bytes[i] = static_cast<std::uint8_t>((high << 4) | low);
  }
  return true;
}

bool execute(std::string_view task, std::uint32_t passes, const qf_block_t &block,
             std::int64_t &result) {
  if (task == "add") {
    result = qf_task_add(2, 3);
  } else if (task == "crc32") {
    result = qf_task_crc32(block);
  } else if (task == "fir") {
    result = qf_task_fir(block, passes);
  } else {
    return false;
  }
  return true;
}

}  // namespace

int main() {
  std::ios::sync_with_stdio(false);
  std::string line;
  qf_block_t block{};
  while (std::getline(std::cin, line)) {
    std::istringstream fields(line);
    std::uint64_t invocation_id = 0;
    std::string task;
    std::uint32_t passes = 0;
    std::string hex;
    if (!(fields >> invocation_id >> task >> passes >> hex) || passes < 1 ||
        !decode_block(hex, block)) {
      std::cout << "error invalid-request" << std::endl;
      continue;
    }
    std::int64_t result = 0;
    const auto started = std::chrono::steady_clock::now();
    const bool known = execute(task, passes, block, result);
    const auto finished = std::chrono::steady_clock::now();
    if (!known) {
      std::cout << "error unknown-task" << std::endl;
      continue;
    }
    const auto local_ns =
        std::chrono::duration_cast<std::chrono::nanoseconds>(finished - started).count();
    std::cout << invocation_id << ' ' << result << ' ' << local_ns << std::endl;
  }
  return 0;
}
