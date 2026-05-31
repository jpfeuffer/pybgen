
#include <cstring>
#include <ctime>
#include <sstream>
#include <iomanip>
#include <algorithm>
#include <stdexcept>

#include <openssl/hmac.h>
#include <openssl/sha.h>

#include "s3_auth.h"

namespace bgen {
namespace s3 {

std::string SigV4::get_amz_date() {
    time_t now = time(nullptr);
    struct tm tm_buf;
#ifdef _WIN32
    gmtime_s(&tm_buf, &now);
#else
    gmtime_r(&now, &tm_buf);
#endif
    char buf[32];
    strftime(buf, sizeof(buf), "%Y%m%dT%H%M%SZ", &tm_buf);
    return std::string(buf);
}

std::string SigV4::get_date_stamp() {
    time_t now = time(nullptr);
    struct tm tm_buf;
#ifdef _WIN32
    gmtime_s(&tm_buf, &now);
#else
    gmtime_r(&now, &tm_buf);
#endif
    char buf[16];
    strftime(buf, sizeof(buf), "%Y%m%d", &tm_buf);
    return std::string(buf);
}

std::vector<unsigned char> SigV4::sha256_raw(const std::string& data) {
    std::vector<unsigned char> hash(SHA256_DIGEST_LENGTH);
    SHA256(reinterpret_cast<const unsigned char*>(data.c_str()), data.size(), hash.data());
    return hash;
}

std::string SigV4::to_hex(const std::vector<unsigned char>& data) {
    std::ostringstream ss;
    for (unsigned char byte : data) {
        ss << std::hex << std::setfill('0') << std::setw(2) << (int)byte;
    }
    return ss.str();
}

std::string SigV4::sha256_hex(const std::string& data) {
    return to_hex(sha256_raw(data));
}

std::string SigV4::empty_payload_hash() {
    return sha256_hex("");
}

std::vector<unsigned char> SigV4::hmac_sha256(
    const std::vector<unsigned char>& key,
    const std::string& data) {
    
    unsigned char result[EVP_MAX_MD_SIZE];
    unsigned int result_len = 0;
    
    HMAC(EVP_sha256(),
         key.data(), static_cast<int>(key.size()),
         reinterpret_cast<const unsigned char*>(data.c_str()),
         data.size(),
         result, &result_len);
    
    return std::vector<unsigned char>(result, result + result_len);
}

std::string SigV4::uri_encode(const std::string& str, bool encode_slash) {
    std::ostringstream encoded;
    for (char c : str) {
        if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
            (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '~' || c == '.') {
            encoded << c;
        } else if (c == '/' && !encode_slash) {
            encoded << c;
        } else {
            encoded << '%' << std::uppercase << std::hex << std::setfill('0')
                    << std::setw(2) << (int)(unsigned char)c;
        }
    }
    return encoded.str();
}

std::string SigV4::get_path(const std::string& url) {
    // Find path after host
    size_t pos = url.find("://");
    if (pos == std::string::npos) return "/";
    pos = url.find('/', pos + 3);
    if (pos == std::string::npos) return "/";
    
    size_t query_pos = url.find('?', pos);
    if (query_pos != std::string::npos) {
        return url.substr(pos, query_pos - pos);
    }
    return url.substr(pos);
}

std::string SigV4::get_query(const std::string& url) {
    size_t pos = url.find('?');
    if (pos == std::string::npos) return "";
    return url.substr(pos + 1);
}

std::string SigV4::sign_request(
    const std::string& method,
    const std::string& url,
    const std::map<std::string, std::string>& headers,
    const std::string& payload_hash,
    const std::string& region,
    const std::string& service,
    const Credentials& credentials,
    const std::string& date_stamp,
    const std::string& amz_date) {
    
    // Step 1: Create canonical request
    std::string canonical_uri = uri_encode(get_path(url), false);
    std::string canonical_querystring = get_query(url);
    
    // Create sorted headers
    std::string canonical_headers;
    std::string signed_headers;
    std::vector<std::pair<std::string, std::string>> sorted_headers(headers.begin(), headers.end());
    std::sort(sorted_headers.begin(), sorted_headers.end());
    
    for (const auto& h : sorted_headers) {
        std::string lower_key = h.first;
        std::transform(lower_key.begin(), lower_key.end(), lower_key.begin(), ::tolower);
        canonical_headers += lower_key + ":" + h.second + "\n";
        if (!signed_headers.empty()) signed_headers += ";";
        signed_headers += lower_key;
    }
    
    std::string canonical_request = method + "\n" +
        canonical_uri + "\n" +
        canonical_querystring + "\n" +
        canonical_headers + "\n" +
        signed_headers + "\n" +
        payload_hash;
    
    // Step 2: Create string to sign
    std::string algorithm = "AWS4-HMAC-SHA256";
    std::string credential_scope = date_stamp + "/" + region + "/" + service + "/aws4_request";
    
    std::string string_to_sign = algorithm + "\n" +
        amz_date + "\n" +
        credential_scope + "\n" +
        sha256_hex(canonical_request);
    
    // Step 3: Calculate signature
    std::string key_str = "AWS4" + credentials.secret_key;
    std::vector<unsigned char> key_bytes(key_str.begin(), key_str.end());
    
    auto k_date = hmac_sha256(key_bytes, date_stamp);
    auto k_region = hmac_sha256(k_date, region);
    auto k_service = hmac_sha256(k_region, service);
    auto k_signing = hmac_sha256(k_service, "aws4_request");
    
    auto signature = hmac_sha256(k_signing, string_to_sign);
    std::string signature_hex = to_hex(signature);
    
    // Step 4: Build authorization header
    std::string authorization = algorithm + " " +
        "Credential=" + credentials.access_key + "/" + credential_scope + ", " +
        "SignedHeaders=" + signed_headers + ", " +
        "Signature=" + signature_hex;
    
    return authorization;
}

} // namespace s3
} // namespace bgen
