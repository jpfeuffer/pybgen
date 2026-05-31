#ifndef BGEN_S3_STREAM_H_
#define BGEN_S3_STREAM_H_

#include <iostream>
#include <vector>
#include <string>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <mutex>

#include "s3_config.h"
#include "s3_credentials.h"
#include "s3_auth.h"

namespace bgen {
namespace s3 {

/// A streambuf that reads from S3 using HTTP Range requests via libcurl.
///
/// This implements std::streambuf so it can be used with std::istream,
/// providing a transparent S3-backed stream that works with the existing
/// bgen reader code.
class S3StreamBuf : public std::streambuf {
public:
    /// Constructor
    ///
    /// @param url Parsed S3 URL
    /// @param config S3 configuration
    /// @param credentials Credential provider
    /// @param buffer_size Size of internal read buffer (default 256KB)
    S3StreamBuf(const S3Url& url, const S3Config& config,
                std::shared_ptr<CredentialProvider> credentials,
                size_t buffer_size = 256 * 1024);
    
    ~S3StreamBuf() override;
    
    /// Get the total size of the remote object
    uint64_t file_size() const { return file_size_; }
    
    /// Whether this stream uses anonymous (unsigned) requests
    bool is_anonymous() const { return anonymous_; }
    
protected:
    /// Override: read more data from S3
    int_type underflow() override;
    
    /// Override: seek to a position in the stream
    pos_type seekoff(off_type off, std::ios_base::seekdir way,
                     std::ios_base::openmode which) override;
    
    /// Override: seek to an absolute position
    pos_type seekpos(pos_type pos, std::ios_base::openmode which) override;
    
private:
    /// Fetch a range of bytes from S3 into the internal buffer
    bool fetch_range(uint64_t start, uint64_t length);
    
    /// Get the file size via HEAD request
    uint64_t get_file_size();
    
    /// Perform an HTTP request with retries (reuses curl session)
    bool perform_request(const std::string& url,
                        const std::vector<std::string>& headers,
                        std::vector<char>& response_body,
                        long& http_code,
                        bool head_only = false,
                        uint64_t* content_length = nullptr);
    
    /// Build signed headers for a request (empty for anonymous)
    std::vector<std::string> build_signed_headers(
        const std::string& method,
        const std::string& range_header = "");
    
    /// Initialize or reset the persistent curl handle
    void ensure_curl_handle();
    
    S3Url url_;
    S3Config config_;
    std::shared_ptr<CredentialProvider> credentials_;
    bool anonymous_;
    
    std::vector<char> buffer_;
    size_t buffer_size_;
    uint64_t file_size_;
    uint64_t current_pos_;    // logical position in file
    uint64_t buffer_start_;   // file offset where buffer starts
    
    void* curl_handle_;       // persistent CURL* handle for session reuse
};

/// An istream backed by S3, for use with the bgen reader.
///
/// Usage:
///   auto stream = S3Stream::open("s3://bucket/file.bgen");
///   // use stream.get() as std::istream*
class S3Stream : public std::istream {
public:
    S3Stream(const S3Url& url, const S3Config& config,
             std::shared_ptr<CredentialProvider> credentials,
             size_t buffer_size = 256 * 1024);
    
    /// Convenience factory: open an S3 URL with default config
    static std::unique_ptr<S3Stream> open(const std::string& s3_url);
    
    /// Convenience factory: open an S3 URL with explicit config
    static std::unique_ptr<S3Stream> open(const std::string& s3_url,
                                          const S3Config& config);
    
    /// Convenience factory: open with explicit credentials
    static std::unique_ptr<S3Stream> open(const std::string& s3_url,
                                          const S3Config& config,
                                          std::shared_ptr<CredentialProvider> credentials);
    
    /// Get the file size
    uint64_t file_size() const { return buf_->file_size(); }
    
private:
    std::unique_ptr<S3StreamBuf> buf_;
};

} // namespace s3
} // namespace bgen

#endif  // BGEN_S3_STREAM_H_
