#ifndef BGEN_S3_AUTH_H_
#define BGEN_S3_AUTH_H_

#include <string>
#include <vector>
#include <map>
#include <algorithm>
#include <ctime>
#include <cstring>
#include <sstream>
#include <iomanip>

#include "s3_credentials.h"

namespace bgen {
namespace s3 {

/// HMAC-SHA256 and SHA256 utilities for AWS Signature V4
class SigV4 {
public:
    /// Sign a request using AWS Signature Version 4
    ///
    /// @param method HTTP method (GET, HEAD, etc.)
    /// @param url Full URL being requested
    /// @param headers Map of headers (host, x-amz-date, etc.)
    /// @param payload_hash SHA256 hash of the request body (empty string hash for GET)
    /// @param region AWS region
    /// @param service AWS service (s3)
    /// @param credentials AWS credentials to sign with
    /// @return Authorization header value
    static std::string sign_request(
        const std::string& method,
        const std::string& url,
        const std::map<std::string, std::string>& headers,
        const std::string& payload_hash,
        const std::string& region,
        const std::string& service,
        const Credentials& credentials,
        const std::string& date_stamp,
        const std::string& amz_date);
    
    /// Get SHA256 hash of data as hex string
    static std::string sha256_hex(const std::string& data);
    
    /// Get SHA256 hash of empty string
    static std::string empty_payload_hash();
    
    /// Get current UTC timestamp in ISO 8601 format (YYYYMMDDTHHmmSSZ)
    static std::string get_amz_date();
    
    /// Get current UTC date stamp (YYYYMMDD)
    static std::string get_date_stamp();
    
private:
    /// HMAC-SHA256
    static std::vector<unsigned char> hmac_sha256(
        const std::vector<unsigned char>& key,
        const std::string& data);
    
    /// SHA256 of raw bytes
    static std::vector<unsigned char> sha256_raw(const std::string& data);
    
    /// Convert bytes to hex string
    static std::string to_hex(const std::vector<unsigned char>& data);
    
    /// URL-encode a string (for canonical URI/query)
    static std::string uri_encode(const std::string& str, bool encode_slash = true);
    
    /// Extract the path from a URL
    static std::string get_path(const std::string& url);
    
    /// Extract query string from URL
    static std::string get_query(const std::string& url);
};

} // namespace s3
} // namespace bgen

#endif  // BGEN_S3_AUTH_H_
