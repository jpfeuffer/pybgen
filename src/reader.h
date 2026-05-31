#ifndef BGEN_READER_H_
#define BGEN_READER_H_

#include <fstream>
#include <stdexcept>
#include <vector>
#include <memory>

#include "header.h"
#include "samples.h"
#include "variant.h"
#include "s3_config.h"

namespace bgen {

class CppBgenReader {
  bool is_stdin = false;
  bool is_s3 = false;
  std::unique_ptr<std::istream> owned_handle_;  // for S3 streams we own
public:
  CppBgenReader(std::string path, std::string sample_path = "", bool delay_parsing = false);
  CppBgenReader(std::string path, std::string sample_path, bool delay_parsing,
                std::string region, std::string endpoint, std::string profile,
                bool use_ssl, bool path_style, bool no_sign_request);
  void parse_all_variants();
  Variant next_var();
  void drop_variants(std::vector<int> indices);
  std::istream * handle;
  std::vector<std::string> varids();
  std::vector<std::string> rsids();
  std::vector<std::string> chroms();
  std::vector<std::uint32_t> positions();
  Variant & operator[](std::size_t idx) { return variants[idx]; }
  Variant & get(std::size_t idx) { return variants[idx]; }
  std::vector<Variant> variants;
  Header header;
  Samples samples;
  std::uint64_t offset;

private:
  void init(std::string path, std::string sample_path, bool delay_parsing,
            const s3::S3Config& config);
};

} // namespace bgen

#endif  // BGEN_READER_H_
