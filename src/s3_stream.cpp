
#include <curl/curl.h>
#include <cstring>
#include <thread>
#include <chrono>
#include <algorithm>
#include <stdexcept>

#include "s3_stream.h"

namespace bgen {
namespace s3 {

// cURL write callback
static size_t write_callback(void* contents, size_t size, size_t nmemb, void* userp) {
    size_t total = size * nmemb;
    auto* vec = static_cast<std::vector<char>*>(userp);
    vec->insert(vec->end(), static_cast<char*>(contents),
                static_cast<char*>(contents) + total);
    return total;
}

// cURL header callback for extracting content-length from HEAD
static size_t header_callback(char* buffer, size_t size, size_t nitems, void* userp) {
    size_t total = size * nitems;
    auto* content_length = static_cast<uint64_t*>(userp);
    
    std::string header(buffer, total);
    // Case-insensitive check for content-length header
    std::string lower = header;
    std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);
    
    if (lower.find("content-length:") == 0) {
        size_t colon_pos = header.find(':');
        std::string value = header.substr(colon_pos + 1);
        // Trim whitespace
        size_t start = value.find_first_not_of(" \t\r\n");
        size_t end = value.find_last_not_of(" \t\r\n");
        if (start != std::string::npos && end != std::string::npos) {
            *content_length = std::stoull(value.substr(start, end - start + 1));
        }
    }
    return total;
}

S3StreamBuf::S3StreamBuf(const S3Url& url, const S3Config& config,
                         std::shared_ptr<CredentialProvider> credentials,
                         size_t buffer_size)
    : url_(url), config_(config), credentials_(credentials),
      anonymous_(config.no_sign_request),
      buffer_size_(buffer_size), current_pos_(0), buffer_start_(0),
      curl_handle_(nullptr) {
    
    buffer_.resize(buffer_size_);
    
    // Initialize with empty buffer state
    setg(buffer_.data(), buffer_.data(), buffer_.data());
    
    // Initialize persistent curl handle
    ensure_curl_handle();
    
    // Get file size
    file_size_ = get_file_size();
    if (file_size_ == 0) {
        throw std::runtime_error("S3 object has zero size or does not exist: " +
                                url_.to_http_url());
    }
}

S3StreamBuf::~S3StreamBuf() {
    if (curl_handle_) {
        curl_easy_cleanup(static_cast<CURL*>(curl_handle_));
        curl_handle_ = nullptr;
    }
}

void S3StreamBuf::ensure_curl_handle() {
    if (!curl_handle_) {
        curl_handle_ = curl_easy_init();
        if (!curl_handle_) {
            throw std::runtime_error("Failed to initialize cURL");
        }
    }
}

std::vector<std::string> S3StreamBuf::build_signed_headers(
    const std::string& method,
    const std::string& range_header) {
    
    std::vector<std::string> curl_headers;
    std::string host = url_.host();
    curl_headers.push_back("Host: " + host);
    
    // For anonymous (no-sign-request) mode, skip signing entirely
    if (anonymous_) {
        if (!range_header.empty()) {
            curl_headers.push_back("Range: " + range_header);
        }
        return curl_headers;
    }
    
    Credentials creds = credentials_->get_credentials();
    if (!creds.is_valid()) {
        throw std::runtime_error("No valid AWS credentials found");
    }
    
    std::string amz_date = SigV4::get_amz_date();
    std::string date_stamp = SigV4::get_date_stamp();
    std::string payload_hash = SigV4::empty_payload_hash();
    
    std::map<std::string, std::string> headers_map;
    headers_map["host"] = host;
    headers_map["x-amz-content-sha256"] = payload_hash;
    headers_map["x-amz-date"] = amz_date;
    
    if (!creds.session_token.empty()) {
        headers_map["x-amz-security-token"] = creds.session_token;
    }
    
    if (!range_header.empty()) {
        headers_map["range"] = range_header;
    }
    
    std::string http_url = url_.to_http_url();
    std::string auth = SigV4::sign_request(
        method, http_url, headers_map, payload_hash,
        url_.region, "s3", creds, date_stamp, amz_date);
    
    curl_headers.push_back("x-amz-content-sha256: " + payload_hash);
    curl_headers.push_back("x-amz-date: " + amz_date);
    curl_headers.push_back("Authorization: " + auth);
    
    if (!creds.session_token.empty()) {
        curl_headers.push_back("x-amz-security-token: " + creds.session_token);
    }
    
    if (!range_header.empty()) {
        curl_headers.push_back("Range: " + range_header);
    }
    
    return curl_headers;
}

bool S3StreamBuf::perform_request(const std::string& url,
                                  const std::vector<std::string>& headers,
                                  std::vector<char>& response_body,
                                  long& http_code,
                                  bool head_only,
                                  uint64_t* content_length) {
    
    for (int attempt = 0; attempt <= config_.max_retries; ++attempt) {
        if (attempt > 0) {
            // Exponential backoff
            int delay = config_.retry_delay_ms * (1 << (attempt - 1));
            std::this_thread::sleep_for(std::chrono::milliseconds(delay));
        }
        
        response_body.clear();
        
        ensure_curl_handle();
        CURL* curl = static_cast<CURL*>(curl_handle_);
        
        // Reset handle state for reuse (keeps connection alive)
        curl_easy_reset(curl);
        
        curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
        curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, (long)config_.connect_timeout_ms);
        curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, (long)config_.request_timeout_ms);
        
        // Enable TCP keepalive and connection reuse
        curl_easy_setopt(curl, CURLOPT_TCP_KEEPALIVE, 1L);
        curl_easy_setopt(curl, CURLOPT_TCP_KEEPIDLE, 120L);
        curl_easy_setopt(curl, CURLOPT_TCP_KEEPINTVL, 60L);
        
        // Enable cookie engine for session reuse (in-memory)
        curl_easy_setopt(curl, CURLOPT_COOKIEFILE, "");
        
        if (head_only) {
            curl_easy_setopt(curl, CURLOPT_NOBODY, 1L);
            if (content_length) {
                curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, header_callback);
                curl_easy_setopt(curl, CURLOPT_HEADERDATA, content_length);
            }
        } else {
            curl_easy_setopt(curl, CURLOPT_HTTPGET, 1L);
            curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback);
            curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response_body);
        }
        
        // Set headers
        struct curl_slist* header_list = nullptr;
        for (const auto& h : headers) {
            header_list = curl_slist_append(header_list, h.c_str());
        }
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, header_list);
        
        // SSL verification (disable for non-SSL endpoints like local Minio)
        if (!config_.use_ssl) {
            curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 0L);
            curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 0L);
        }
        
        CURLcode res = curl_easy_perform(curl);
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
        
        curl_slist_free_all(header_list);
        
        if (res != CURLE_OK) {
            if (attempt == config_.max_retries) {
                return false;
            }
            continue;
        }
        
        // Retry on 5xx errors
        if (http_code >= 500) {
            if (attempt == config_.max_retries) {
                return false;
            }
            continue;
        }
        
        return true;
    }
    return false;
}

uint64_t S3StreamBuf::get_file_size() {
    std::string http_url = url_.to_http_url();
    auto headers = build_signed_headers("HEAD");
    
    std::vector<char> response;
    long http_code = 0;
    uint64_t content_length = 0;
    
    bool ok = perform_request(http_url, headers, response, http_code,
                             true, &content_length);
    
    if (!ok || http_code != 200) {
        throw std::runtime_error("Failed to get S3 object size (HTTP " +
                                std::to_string(http_code) + "): " + http_url);
    }
    
    return content_length;
}

bool S3StreamBuf::fetch_range(uint64_t start, uint64_t length) {
    if (start >= file_size_) return false;
    
    // Clamp to file size
    if (start + length > file_size_) {
        length = file_size_ - start;
    }
    
    std::string range = "bytes=" + std::to_string(start) + "-" +
                       std::to_string(start + length - 1);
    
    std::string http_url = url_.to_http_url();
    auto headers = build_signed_headers("GET", range);
    
    std::vector<char> response;
    long http_code = 0;
    
    bool ok = perform_request(http_url, headers, response, http_code);
    
    if (!ok || (http_code != 200 && http_code != 206)) {
        return false;
    }
    
    // Copy to internal buffer
    size_t bytes_read = response.size();
    if (bytes_read > buffer_.size()) {
        buffer_.resize(bytes_read);
    }
    std::memcpy(buffer_.data(), response.data(), bytes_read);
    
    buffer_start_ = start;
    current_pos_ = start;
    
    // Set the get area
    setg(buffer_.data(), buffer_.data(), buffer_.data() + bytes_read);
    
    return true;
}

S3StreamBuf::int_type S3StreamBuf::underflow() {
    if (gptr() < egptr()) {
        return traits_type::to_int_type(*gptr());
    }
    
    // Calculate position based on how far we've read
    uint64_t next_pos = buffer_start_ + (gptr() - eback());
    
    if (next_pos >= file_size_) {
        return traits_type::eof();
    }
    
    // Fetch next chunk
    uint64_t fetch_size = std::min((uint64_t)buffer_size_, file_size_ - next_pos);
    if (!fetch_range(next_pos, fetch_size)) {
        return traits_type::eof();
    }
    
    return traits_type::to_int_type(*gptr());
}

S3StreamBuf::pos_type S3StreamBuf::seekoff(off_type off, std::ios_base::seekdir way,
                                           std::ios_base::openmode which) {
    uint64_t new_pos;
    
    switch (way) {
        case std::ios_base::beg:
            new_pos = off;
            break;
        case std::ios_base::cur:
            new_pos = buffer_start_ + (gptr() - eback()) + off;
            break;
        case std::ios_base::end:
            new_pos = file_size_ + off;
            break;
        default:
            return pos_type(off_type(-1));
    }
    
    if (new_pos > file_size_) {
        return pos_type(off_type(-1));
    }
    
    // Check if the target position is within our current buffer
    uint64_t buf_end = buffer_start_ + (egptr() - eback());
    if (new_pos >= buffer_start_ && new_pos < buf_end) {
        // Just adjust the get pointer
        setg(eback(), eback() + (new_pos - buffer_start_), egptr());
        return pos_type(new_pos);
    }
    
    // Need to fetch new data
    uint64_t fetch_size = std::min((uint64_t)buffer_size_, file_size_ - new_pos);
    if (fetch_size > 0 && !fetch_range(new_pos, fetch_size)) {
        return pos_type(off_type(-1));
    }
    
    // Handle seeking to EOF
    if (new_pos == file_size_) {
        buffer_start_ = new_pos;
        setg(buffer_.data(), buffer_.data(), buffer_.data());
    }
    
    return pos_type(new_pos);
}

S3StreamBuf::pos_type S3StreamBuf::seekpos(pos_type pos, std::ios_base::openmode which) {
    return seekoff(off_type(pos), std::ios_base::beg, which);
}

// S3Stream implementation

S3Stream::S3Stream(const S3Url& url, const S3Config& config,
                   std::shared_ptr<CredentialProvider> credentials,
                   size_t buffer_size)
    : std::istream(nullptr) {
    buf_ = std::unique_ptr<S3StreamBuf>(new S3StreamBuf(url, config, credentials, buffer_size));
    rdbuf(buf_.get());
}

std::unique_ptr<S3Stream> S3Stream::open(const std::string& s3_url) {
    S3Config config = S3Config::from_env();
    return open(s3_url, config);
}

std::unique_ptr<S3Stream> S3Stream::open(const std::string& s3_url, const S3Config& config) {
    S3Url url = parse_s3_url(s3_url, config);
    
    std::shared_ptr<CredentialProvider> credentials;
    if (config.no_sign_request) {
        credentials = std::make_shared<AnonymousCredentialProvider>();
    } else if (!config.profile.empty()) {
        credentials = std::make_shared<CredentialChain>(config.profile);
    } else {
        credentials = std::make_shared<CredentialChain>();
    }
    
    return std::unique_ptr<S3Stream>(new S3Stream(url, config, credentials));
}

std::unique_ptr<S3Stream> S3Stream::open(const std::string& s3_url,
                                         const S3Config& config,
                                         std::shared_ptr<CredentialProvider> credentials) {
    S3Url url = parse_s3_url(s3_url, config);
    return std::unique_ptr<S3Stream>(new S3Stream(url, config, credentials));
}

} // namespace s3
} // namespace bgen
