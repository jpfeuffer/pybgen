#ifndef BGEN_S3_CONFIG_H_
#define BGEN_S3_CONFIG_H_

#include <string>
#include <cstdlib>

namespace bgen {
namespace s3 {

/// Parsed S3 URL components
struct S3Url {
    std::string bucket;
    std::string key;
    std::string region;
    std::string endpoint;  // custom endpoint (e.g. Minio)
    bool use_ssl = true;
    bool path_style = false;  // path-style addressing (required for Minio)
    
    /// Construct the full HTTP URL for the object
    std::string to_http_url() const {
        std::string scheme = use_ssl ? "https" : "http";
        if (!endpoint.empty()) {
            if (path_style) {
                return scheme + "://" + endpoint + "/" + bucket + "/" + key;
            } else {
                return scheme + "://" + bucket + "." + endpoint + "/" + key;
            }
        }
        // Default AWS S3 URL
        return scheme + "://" + bucket + ".s3." + region + ".amazonaws.com/" + key;
    }
    
    /// Get the host header value
    std::string host() const {
        if (!endpoint.empty()) {
            if (path_style) {
                return endpoint;
            }
            return bucket + "." + endpoint;
        }
        return bucket + ".s3." + region + ".amazonaws.com";
    }
};

/// Configuration for S3 access
struct S3Config {
    std::string region = "us-east-1";
    std::string endpoint;       // custom endpoint override
    std::string profile;        // AWS profile name (empty = use default chain)
    bool use_ssl = true;
    bool path_style = false;    // use path-style addressing
    bool no_sign_request = false; // skip signing for public buckets
    int connect_timeout_ms = 5000;
    int request_timeout_ms = 30000;
    int max_retries = 3;
    int retry_delay_ms = 100;   // base delay, exponential backoff applied
    
    /// Load configuration from environment variables
    static S3Config from_env() {
        S3Config cfg;
        const char* region = std::getenv("AWS_DEFAULT_REGION");
        if (!region) region = std::getenv("AWS_REGION");
        if (region) cfg.region = region;
        
        const char* endpoint = std::getenv("AWS_ENDPOINT_URL");
        if (!endpoint) endpoint = std::getenv("BGEN_S3_ENDPOINT");
        if (endpoint) cfg.endpoint = endpoint;
        
        const char* use_ssl = std::getenv("BGEN_S3_USE_SSL");
        if (use_ssl) {
            std::string val(use_ssl);
            cfg.use_ssl = (val == "1" || val == "true" || val == "TRUE");
        }
        
        const char* path_style = std::getenv("BGEN_S3_PATH_STYLE");
        if (!path_style) path_style = std::getenv("AWS_S3_FORCE_PATH_STYLE");
        if (path_style) {
            std::string val(path_style);
            cfg.path_style = (val == "1" || val == "true" || val == "TRUE");
        }
        
        const char* no_sign = std::getenv("BGEN_S3_NO_SIGN_REQUEST");
        if (!no_sign) no_sign = std::getenv("AWS_NO_SIGN_REQUEST");
        if (no_sign) {
            std::string val(no_sign);
            cfg.no_sign_request = (val == "1" || val == "true" || val == "TRUE");
        }
        
        const char* profile = std::getenv("AWS_PROFILE");
        if (profile) cfg.profile = profile;
        
        return cfg;
    }
};

/// Parse an s3:// URL into components
///
/// Formats supported:
///   s3://bucket/key
///   s3://bucket/path/to/key
///
/// Region and endpoint are filled from config.
inline S3Url parse_s3_url(const std::string& url, const S3Config& config) {
    S3Url result;
    
    // Strip s3:// prefix
    std::string path = url.substr(5);  // skip "s3://"
    
    // Split bucket and key
    size_t slash_pos = path.find('/');
    if (slash_pos == std::string::npos) {
        result.bucket = path;
        result.key = "";
    } else {
        result.bucket = path.substr(0, slash_pos);
        result.key = path.substr(slash_pos + 1);
    }
    
    result.region = config.region;
    result.endpoint = config.endpoint;
    result.use_ssl = config.use_ssl;
    result.path_style = config.path_style;
    
    return result;
}

/// Check if a path looks like an S3 URL
inline bool is_s3_url(const std::string& path) {
    return path.size() > 5 && path.substr(0, 5) == "s3://";
}

} // namespace s3
} // namespace bgen

#endif  // BGEN_S3_CONFIG_H_
