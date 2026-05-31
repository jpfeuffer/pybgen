# S3 support architecture plan (modern C++)

This document proposes a concrete, implementation-oriented architecture for adding native S3-backed reads while keeping existing local-file behavior and API usage stable.

## 1) Design principles

- Keep `BgenReader` public behavior stable; add S3 support behind internal interfaces.
- Separate concerns: URI parsing, credentials, signing, transport, retries, caching, stream semantics.
- Prefer RAII, value types, `std::unique_ptr`, `std::shared_ptr`, `std::optional`, `std::chrono`.
- Keep S3/auth/cURL details out of `Header`, `Variant`, `CppBgenReader` logic.
- Preserve portability across Linux/macOS/Windows.

## 2) New module layout

Add a new C++ subtree:

- `src/io/uri.h|cpp` – URI parsing (`file://`, `s3://`, plain paths)
- `src/io/reader_source.h` – common random-access interface
- `src/io/local_file_source.h|cpp` – local file implementation
- `src/io/range_reader.h|cpp` – buffered range abstraction on top of remote object access
- `src/s3/s3_config.h` – config structs
- `src/s3/credentials.h|cpp` – credential model + provider chain
- `src/s3/sigv4.h|cpp` – AWS Signature V4 signing
- `src/http/http_client.h` – HTTP transport abstraction
- `src/http/curl_http_client.h|cpp` – libcurl implementation
- `src/s3/s3_object_store.h|cpp` – S3 object access using HTTP + SigV4
- `src/common/error.h` – structured error type and categories

This keeps the existing domain classes unchanged and moves new complexity into isolated modules.

## 3) Core interfaces

### 3.1 Random access source (shared usage model)

```cpp
// src/io/reader_source.h
namespace bgen::io {

/// Abstract random-access byte source.
/// Both local files and remote object stores implement this interface,
/// giving the parser a unified read model.
class IRandomAccessSource {
public:
  virtual ~IRandomAccessSource() = default;

  /// Total size of the underlying object/file in bytes.
  virtual std::uint64_t size() const = 0;

  /// Read exactly `out.size()` bytes starting at `offset`.
  /// Throws BgenError on short read or I/O failure.
  virtual void read_exact(std::uint64_t offset, std::span<std::byte> out) = 0;

  /// Hint: source supports giving a const window into an internal buffer
  /// (useful for mmap or large prefetch buffers). Default false.
  virtual bool supports_zero_copy_window() const { return false; }

  /// Optional: expose a std::istream adapter for legacy code paths
  /// that still use stream-based parsing (Header, Samples).
  virtual std::unique_ptr<std::istream> as_istream();
};

} // namespace bgen::io
```

`CppBgenReader` stores `std::shared_ptr<IRandomAccessSource>` instead of owning `std::ifstream` directly.

Concrete implementations:

```cpp
// src/io/local_file_source.h
namespace bgen::io {

class LocalFileSource final : public IRandomAccessSource {
public:
  explicit LocalFileSource(const std::string& path);
  std::uint64_t size() const override;
  void read_exact(std::uint64_t offset, std::span<std::byte> out) override;
  std::unique_ptr<std::istream> as_istream() override;

private:
  std::string path_;
  std::ifstream stream_;
  std::uint64_t file_size_;
};

/// Wraps std::cin for stdin streaming mode (no seeking).
class StdinSource final : public IRandomAccessSource {
public:
  StdinSource();
  std::uint64_t size() const override;  // throws — size unknown for stdin
  void read_exact(std::uint64_t offset, std::span<std::byte> out) override;
  std::unique_ptr<std::istream> as_istream() override;

private:
  std::vector<std::byte> buffer_;  // accumulates bytes read so far
  std::uint64_t pos_ = 0;
};

} // namespace bgen::io
```

```cpp
// src/io/range_reader_source.h
namespace bgen::io {

/// Remote random-access source built on top of an IObjectStore + BlockCache.
class RangeReaderSource final : public IRandomAccessSource {
public:
  struct Options {
    std::size_t block_size = 256 * 1024;    // 256 KiB aligned blocks
    std::size_t max_cached_blocks = 64;     // ~16 MiB default footprint
    bool coalesce_adjacent = true;          // merge nearby block misses
    std::size_t coalesce_gap_limit = 32768; // max gap to bridge in one request
  };

  RangeReaderSource(std::shared_ptr<IObjectStore> store,
                    ObjectRef object,
                    Options opts = {});

  std::uint64_t size() const override;
  void read_exact(std::uint64_t offset, std::span<std::byte> out) override;

private:
  std::shared_ptr<IObjectStore> store_;
  ObjectRef object_;
  Options opts_;
  std::uint64_t object_size_;
  std::unique_ptr<BlockCache> cache_;

  void fetch_blocks(std::uint64_t first_block, std::uint64_t last_block);
};

} // namespace bgen::io
```

### 3.2 Connection / session management

```cpp
// src/http/http_client.h
namespace bgen::http {

/// TLS and connection configuration, resolved once at client creation.
struct TlsConfig {
  bool verify_peer = true;          // CURLOPT_SSL_VERIFYPEER
  bool verify_host = true;          // CURLOPT_SSL_VERIFYHOST
  std::string ca_bundle_path;       // override system default CA bundle
  std::string client_cert_path;     // optional mTLS
  std::string client_key_path;
};

/// Per-session connection behaviour.
struct ConnectionConfig {
  std::chrono::milliseconds connect_timeout{5000};
  std::chrono::milliseconds request_timeout{30000};
  std::chrono::milliseconds low_speed_time{15000};
  std::uint32_t low_speed_limit_bytes = 1024;
  bool tcp_keepalive = true;
  std::chrono::seconds keepalive_idle{60};
  std::chrono::seconds keepalive_interval{30};
  std::size_t max_connections_per_host = 4;
  bool http2_enabled = true;        // prefer HTTP/2 multiplexing
};

struct HttpRequest {
  std::string method;               // GET, HEAD, PUT, etc.
  std::string url;
  std::vector<std::pair<std::string, std::string>> headers;
  std::optional<std::pair<std::uint64_t, std::uint64_t>> byte_range;
  std::string body;
  std::chrono::milliseconds timeout_override{0};  // per-request override
};

struct HttpResponse {
  long status_code = 0;
  std::vector<std::pair<std::string, std::string>> headers;
  std::string body;
  std::chrono::milliseconds elapsed{0};
  std::string effective_url;        // after redirects
};

/// Abstract HTTP transport. One instance = one logical session with
/// connection pooling, keepalive, and TLS context reuse.
class IHttpClient {
public:
  virtual ~IHttpClient() = default;

  /// Perform a single request. Thread-safe if implementation allows.
  virtual HttpResponse perform(const HttpRequest& req) = 0;

  /// Batch interface for future parallel prefetch.
  virtual std::vector<HttpResponse> perform_multi(
      const std::vector<HttpRequest>& requests) {
    std::vector<HttpResponse> results;
    results.reserve(requests.size());
    for (auto& r : requests) results.push_back(perform(r));
    return results;
  }

  /// Graceful shutdown: drain in-flight requests, close connections.
  virtual void shutdown() {}
};

} // namespace bgen::http
```

```cpp
// src/http/curl_http_client.h
namespace bgen::http {

/// libcurl-based implementation of IHttpClient.
/// Manages a pool of CURL easy handles (one per logical connection slot).
/// Thread-safety: perform() is safe to call from multiple threads;
/// each thread gets a handle from the pool or blocks until one is free.
class CurlHttpClient final : public IHttpClient {
public:
  CurlHttpClient(TlsConfig tls, ConnectionConfig conn);
  ~CurlHttpClient();

  // Non-copyable, moveable
  CurlHttpClient(const CurlHttpClient&) = delete;
  CurlHttpClient& operator=(const CurlHttpClient&) = delete;
  CurlHttpClient(CurlHttpClient&&) noexcept;
  CurlHttpClient& operator=(CurlHttpClient&&) noexcept;

  HttpResponse perform(const HttpRequest& req) override;
  std::vector<HttpResponse> perform_multi(
      const std::vector<HttpRequest>& requests) override;
  void shutdown() override;

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;  // PIMPL hides all curl headers

  /// RAII singleton for curl_global_init / curl_global_cleanup
  struct CurlGlobalGuard {
    CurlGlobalGuard();
    ~CurlGlobalGuard();
  };
  static CurlGlobalGuard& global_init();
};

/// Internal handle pool (inside Impl)
/// - std::vector<CURL*> idle_handles_;
/// - std::mutex pool_mutex_;
/// - std::condition_variable pool_cv_;
/// - max_size_ = conn.max_connections_per_host
///
/// acquire(): blocks until handle available, applies per-request options.
/// release(): resets handle state, returns to pool.
/// This gives HTTP/1.1 connection reuse and bounded concurrency.

} // namespace bgen::http
```

### 3.3 Authentication layer (detailed)

```cpp
// src/s3/credentials.h
namespace bgen::s3 {

/// Immutable credential snapshot.
struct AwsCredentials {
  std::string access_key_id;
  std::string secret_access_key;
  std::string session_token;  // empty for permanent keys

  /// Expiry time for temporary credentials; nullopt for permanent keys.
  std::optional<std::chrono::system_clock::time_point> expiration;

  bool is_expired() const;
  bool expires_within(std::chrono::seconds margin) const;
};

/// Abstract credential source. Each provider knows one retrieval strategy.
class ICredentialsProvider {
public:
  virtual ~ICredentialsProvider() = default;

  /// Attempt to load credentials. Returns nullopt if this provider
  /// cannot supply credentials (e.g., env vars not set).
  virtual std::optional<AwsCredentials> get() = 0;

  /// Human-readable name for logging/diagnostics.
  virtual std::string_view name() const = 0;
};

/// Tries providers in order; returns first success.
class CredentialsProviderChain final : public ICredentialsProvider {
public:
  explicit CredentialsProviderChain(
      std::vector<std::unique_ptr<ICredentialsProvider>> providers);

  std::optional<AwsCredentials> get() override;
  std::string_view name() const override { return "chain"; }

private:
  std::vector<std::unique_ptr<ICredentialsProvider>> providers_;
};

/// Wraps any provider and caches credentials until near-expiry.
/// Refresh happens synchronously on first call after expiry window.
class CachingCredentialsProvider final : public ICredentialsProvider {
public:
  explicit CachingCredentialsProvider(
      std::unique_ptr<ICredentialsProvider> inner,
      std::chrono::seconds refresh_margin = std::chrono::seconds{300});

  std::optional<AwsCredentials> get() override;
  std::string_view name() const override { return inner_->name(); }

  /// Force refresh on next get() (e.g., after a 403 response).
  void invalidate();

private:
  std::unique_ptr<ICredentialsProvider> inner_;
  std::chrono::seconds refresh_margin_;
  mutable std::mutex mu_;
  std::optional<AwsCredentials> cached_;
};

// --- Concrete providers ---

/// Reads AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN.
class EnvironmentCredentialsProvider final : public ICredentialsProvider {
public:
  std::optional<AwsCredentials> get() override;
  std::string_view name() const override { return "environment"; }
};

/// Parses ~/.aws/credentials and ~/.aws/config (INI format).
class SharedFileCredentialsProvider final : public ICredentialsProvider {
public:
  explicit SharedFileCredentialsProvider(
      std::string profile = "",          // empty => AWS_PROFILE or "default"
      std::string credentials_file = "", // empty => ~/.aws/credentials
      std::string config_file = "");     // empty => ~/.aws/config

  std::optional<AwsCredentials> get() override;
  std::string_view name() const override { return "shared_file"; }

private:
  std::string profile_;
  std::string credentials_path_;
  std::string config_path_;
  std::optional<AwsCredentials> parse_ini(const std::string& path);
};

/// Calls ECS container credentials endpoint (169.254.170.2).
/// Activated when AWS_CONTAINER_CREDENTIALS_RELATIVE_URI is set.
class EcsCredentialsProvider final : public ICredentialsProvider {
public:
  explicit EcsCredentialsProvider(std::shared_ptr<http::IHttpClient> http);
  std::optional<AwsCredentials> get() override;
  std::string_view name() const override { return "ecs_container"; }

private:
  std::shared_ptr<http::IHttpClient> http_;
};

/// EC2 Instance Metadata Service v2 (IMDSv2).
/// PUT to get token, then GET role + credentials.
class ImdsCredentialsProvider final : public ICredentialsProvider {
public:
  explicit ImdsCredentialsProvider(
      std::shared_ptr<http::IHttpClient> http,
      std::chrono::seconds token_ttl = std::chrono::seconds{21600});

  std::optional<AwsCredentials> get() override;
  std::string_view name() const override { return "imds_v2"; }

private:
  std::shared_ptr<http::IHttpClient> http_;
  std::chrono::seconds token_ttl_;
  std::string cached_token_;
  std::chrono::system_clock::time_point token_expiry_;

  std::optional<std::string> get_token();
  std::optional<std::string> get_role(const std::string& token);
  std::optional<AwsCredentials> get_role_credentials(
      const std::string& token, const std::string& role);
};

/// Provides static credentials passed directly by the user.
class StaticCredentialsProvider final : public ICredentialsProvider {
public:
  explicit StaticCredentialsProvider(AwsCredentials creds);
  std::optional<AwsCredentials> get() override;
  std::string_view name() const override { return "static"; }

private:
  AwsCredentials creds_;
};

/// Factory: builds the default provider chain in standard AWS order.
std::unique_ptr<ICredentialsProvider> make_default_credentials_chain(
    std::shared_ptr<http::IHttpClient> http,
    const S3Config& config);

} // namespace bgen::s3
```

```cpp
// src/s3/sigv4.h
namespace bgen::s3 {

/// Computes AWS Signature Version 4 for a given HTTP request.
class SigV4Signer {
public:
  SigV4Signer(std::string region, std::string service = "s3");

  /// Signs the request in-place, adding Authorization, x-amz-date,
  /// x-amz-content-sha256, and optionally x-amz-security-token headers.
  void sign(http::HttpRequest& request, const AwsCredentials& creds) const;

  /// Adjust signing clock by observed skew (from server Date header).
  void set_clock_skew(std::chrono::seconds skew);

private:
  std::string region_;
  std::string service_;
  std::chrono::seconds clock_skew_{0};

  std::string canonical_request(const http::HttpRequest& req,
                                const std::string& payload_hash) const;
  std::string string_to_sign(const std::string& datetime,
                             const std::string& date,
                             const std::string& canonical_req_hash) const;
  std::string signing_key(const std::string& date,
                          const std::string& secret) const;
  std::string credential_scope(const std::string& date) const;

  // HMAC-SHA256 and SHA-256 utilities (implemented in sigv4.cpp)
  static std::string hmac_sha256(const std::string& key,
                                 const std::string& data);
  static std::string sha256_hex(const std::string& data);
};

} // namespace bgen::s3
```

### 3.4 Object store abstraction (future extensibility)

```cpp
// src/s3/s3_object_store.h
namespace bgen::s3 {

/// Identifies an object in a bucket.
struct ObjectRef {
  std::string bucket;
  std::string key;
  std::string version_id;  // optional, for versioned buckets
};

/// Abstract object-store interface. S3, GCS, Azure Blob etc.
class IObjectStore {
public:
  virtual ~IObjectStore() = default;

  /// Get the size of an object (HEAD request).
  virtual std::uint64_t head_size(const ObjectRef& obj) = 0;

  /// Read a byte range [begin, end] inclusive. Returns raw bytes.
  virtual std::string get_range(const ObjectRef& obj,
                                std::uint64_t begin,
                                std::uint64_t end_inclusive) = 0;

  /// Optional: check if object exists without fetching size.
  virtual bool exists(const ObjectRef& obj) { 
    try { head_size(obj); return true; }
    catch (...) { return false; }
  }
};

/// S3-specific implementation combining HTTP + SigV4 + retries.
class S3ObjectStore final : public IObjectStore {
public:
  S3ObjectStore(std::shared_ptr<http::IHttpClient> http,
                std::shared_ptr<ICredentialsProvider> creds,
                S3Config config);

  std::uint64_t head_size(const ObjectRef& obj) override;
  std::string get_range(const ObjectRef& obj,
                        std::uint64_t begin,
                        std::uint64_t end_inclusive) override;

private:
  std::shared_ptr<http::IHttpClient> http_;
  std::shared_ptr<ICredentialsProvider> creds_;
  S3Config config_;
  SigV4Signer signer_;
  RetryPolicy retry_;

  /// Construct full URL from bucket/key/config.
  std::string build_url(const ObjectRef& obj) const;

  /// Execute with signing, retries, and credential refresh.
  http::HttpResponse execute_with_auth(http::HttpRequest req);
};

} // namespace bgen::s3
```

### 3.5 Retry policy

```cpp
// src/s3/retry_policy.h
namespace bgen::s3 {

struct RetryConfig {
  std::uint32_t max_attempts = 4;
  std::chrono::milliseconds base_delay{200};
  std::chrono::milliseconds max_delay{30000};
  double jitter_factor = 0.2;     // ±20% randomized
  bool retry_on_throttle = true;  // HTTP 429
};

class RetryPolicy {
public:
  explicit RetryPolicy(RetryConfig config = {});

  /// Determine if a failed request should be retried.
  struct Decision {
    bool should_retry;
    std::chrono::milliseconds delay;
    bool force_credential_refresh;
  };

  Decision evaluate(std::uint32_t attempt,
                    long http_status,
                    const std::string& error_code) const;

private:
  RetryConfig config_;
  std::chrono::milliseconds compute_delay(std::uint32_t attempt) const;
  bool is_retryable_status(long status) const;
  bool is_retryable_error(const std::string& code) const;
};

} // namespace bgen::s3
```

## 4) Configuration model

### 4.1 S3Config struct

```cpp
// src/s3/s3_config.h
namespace bgen::s3 {

struct S3Config {
  // --- Endpoint ---
  std::string region = "us-east-1";
  std::string endpoint_override;         // e.g. "http://localhost:9000" for MinIO
  bool use_https = true;
  bool path_style_addressing = false;    // true for MinIO / old-style S3
  bool use_dual_stack = false;           // IPv4+IPv6 dual-stack endpoints

  // --- TLS ---
  bool verify_tls = true;
  std::string ca_bundle_path;            // override system CA bundle
  std::string client_cert_path;          // optional mTLS
  std::string client_key_path;

  // --- Timeouts ---
  std::chrono::milliseconds connect_timeout{5000};
  std::chrono::milliseconds request_timeout{30000};

  // --- Retries ---
  std::uint32_t max_retries = 3;
  std::chrono::milliseconds retry_base_delay{200};
  std::chrono::milliseconds retry_max_delay{30000};

  // --- Range/Cache ---
  std::size_t range_block_size = 256 * 1024;   // 256 KiB
  std::size_t range_cache_blocks = 64;          // max cached blocks per reader
  bool prefetch_sequential = true;

  // --- Connection pooling ---
  std::size_t max_connections_per_host = 4;
  bool http2_enabled = true;
  bool tcp_keepalive = true;

  // --- Auth ---
  std::string profile;                   // AWS profile name override
  std::string access_key_id;             // explicit static creds (highest priority)
  std::string secret_access_key;
  std::string session_token;

  /// Build endpoint URL for a given bucket.
  std::string endpoint_for_bucket(const std::string& bucket) const;
};

} // namespace bgen::s3
```

### 4.2 Configuration resolution

```cpp
// src/s3/s3_config.cpp
namespace bgen::s3 {

/// Merge environment variables into config (lower priority than explicit fields).
S3Config resolve_config(S3Config user_config) {
  // Region
  if (user_config.region.empty()) {
    if (auto* v = std::getenv("AWS_REGION")) user_config.region = v;
    else if (auto* v2 = std::getenv("AWS_DEFAULT_REGION")) user_config.region = v2;
    else user_config.region = "us-east-1";
  }
  // Endpoint override
  if (user_config.endpoint_override.empty()) {
    if (auto* v = std::getenv("AWS_ENDPOINT_URL")) user_config.endpoint_override = v;
    else if (auto* v2 = std::getenv("AWS_ENDPOINT_URL_S3")) user_config.endpoint_override = v2;
  }
  // CA bundle
  if (user_config.ca_bundle_path.empty()) {
    if (auto* v = std::getenv("AWS_CA_BUNDLE")) user_config.ca_bundle_path = v;
  }
  return user_config;
}

std::string S3Config::endpoint_for_bucket(const std::string& bucket) const {
  if (!endpoint_override.empty()) return endpoint_override;
  std::string scheme = use_https ? "https" : "http";
  if (path_style_addressing) {
    return scheme + "://s3." + region + ".amazonaws.com/" + bucket;
  }
  return scheme + "://" + bucket + ".s3." + region + ".amazonaws.com";
}

} // namespace bgen::s3
```

### 4.3 URI parsing

```cpp
// src/io/uri.h
namespace bgen::io {

struct ParsedUri {
  enum class Scheme { Local, Stdin, S3, Unknown };

  Scheme scheme = Scheme::Local;
  std::string bucket;         // S3 only
  std::string key;            // S3 only
  std::string local_path;     // file:// or plain path

  static ParsedUri parse(const std::string& input);
  bool is_remote() const { return scheme == Scheme::S3; }
};

} // namespace bgen::io
```

Provide from:

1. explicit constructor options (highest precedence)
2. environment (`AWS_REGION`, endpoint/tls overrides)
3. defaults

## 5) Credential handling

Use provider-chain pattern:

```cpp
class ICredentialsProvider {
public:
  virtual ~ICredentialsProvider() = default;
  virtual std::optional<AwsCredentials> get() = 0;
};
```

Chain order:

1. Static credentials from explicit config
2. Environment (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`)
3. Shared files (`~/.aws/credentials`, `~/.aws/config`, selected profile)
4. ECS task credentials endpoint
5. EC2 IMDSv2 instance profile

`CachingCredentialsProvider` wraps any provider and refreshes before expiration for temporary creds.

### 5.1 Credential lifecycle and refresh

```
┌──────────────────────────────────────────────────────────────┐
│ CachingCredentialsProvider                                    │
│                                                              │
│  get() called                                                │
│    ├── cached_ valid & not near expiry? → return cached_     │
│    ├── cached_ near expiry (< refresh_margin)?               │
│    │     └── lock, call inner_->get(), update cached_        │
│    └── no cached_ at all?                                    │
│          └── lock, call inner_->get(), store as cached_      │
│                                                              │
│  invalidate() called (after 403 response)                    │
│    └── lock, clear cached_, next get() forces refresh        │
└──────────────────────────────────────────────────────────────┘
```

### 5.2 IMDSv2 flow detail

```
1. PUT http://169.254.169.254/latest/api/token
   Headers: X-aws-ec2-metadata-token-ttl-seconds: 21600
   → token (cached for TTL)

2. GET http://169.254.169.254/latest/meta-data/iam/security-credentials/
   Headers: X-aws-ec2-metadata-token: <token>
   → role name

3. GET http://169.254.169.254/latest/meta-data/iam/security-credentials/<role>
   Headers: X-aws-ec2-metadata-token: <token>
   → JSON { AccessKeyId, SecretAccessKey, Token, Expiration }
```

### 5.3 SharedFileCredentialsProvider detail

Parses standard INI format:

```ini
[profile_name]
aws_access_key_id = AKIA...
aws_secret_access_key = wJalr...
aws_session_token = FwoG...    # optional
```

Profile resolution: explicit `profile` param > `AWS_PROFILE` env > `"default"`.
File paths: `credentials_file` param > `AWS_SHARED_CREDENTIALS_FILE` env > `~/.aws/credentials`.

## 6) SigV4 signing

`SigV4Signer` takes request, credentials, region, service=`s3`, and returns signed headers.

Responsibilities:

- canonical request generation
- payload hash (`UNSIGNED-PAYLOAD` for GET/HEAD)
- string-to-sign + HMAC chain
- session-token header passthrough
- clock-skew compensation from response `Date` header on auth failures

Keep all crypto/signing logic in `src/s3/sigv4.*`.

## 7) cURL dependency + SSL/TLS integration

- Add optional build flag: `BGEN_ENABLE_S3` (default OFF initially).
- When ON:
  - detect libcurl via CMake/pkg-config/setuptools extension args
  - compile `src/http/*` and `src/s3/*`
  - link against libcurl
- `CurlHttpClient` should:
  - call `curl_global_init` through RAII singleton
  - use one easy handle per request (simple path), or handle pool if profiling requires
  - set TLS options (`CURLOPT_USE_SSL`, `CURLOPT_SSL_VERIFYPEER`, `CURLOPT_SSL_VERIFYHOST`, `CURLOPT_CAINFO`)
  - disable protocol downgrade and non-HTTP(S) schemes

No TLS knobs should leak into reader/variant code.

## 8) Retries + resilience

Retry policy applies in `S3ObjectStore` (not domain classes):

- retry on: connection errors, DNS transient errors, HTTP 429, 500, 502, 503, 504
- do not retry auth/signature failures (except one forced credential refresh)
- exponential backoff + jitter
- bounded retries and per-request timeout budget

Map final failures to typed errors:

- `ErrorCategory::Auth`, `Network`, `Timeout`, `NotFound`, `InvalidResponse`, `IO`

## 9) Streaming and range reads

Implement `RangeReaderSource` with block cache:

- convert `read_exact(offset, n)` into one/more aligned range GETs
- cache fixed-size blocks (LRU)
- coalesce adjacent misses into one request when beneficial
- support tiny random reads efficiently for header/variant metadata parsing

This preserves the current random-seek usage model with minimal changes to parsing code.

### 9.1 BlockCache detail

```cpp
// src/io/block_cache.h
namespace bgen::io {

/// Fixed-size block of data fetched from remote storage.
struct CacheBlock {
  std::uint64_t block_index;            // which block number this represents
  std::vector<std::byte> data;          // exactly block_size bytes (or less for final block)
  std::chrono::steady_clock::time_point last_access;
};

/// LRU block cache for range-read data.
/// Each block covers a fixed byte range: [block_index * block_size, (block_index+1) * block_size).
class BlockCache {
public:
  struct Stats {
    std::uint64_t hits = 0;
    std::uint64_t misses = 0;
    std::uint64_t evictions = 0;
    std::uint64_t bytes_fetched = 0;
  };

  BlockCache(std::size_t block_size, std::size_t max_blocks);

  /// Lookup a block. Returns nullptr if not cached.
  const CacheBlock* get(std::uint64_t block_index);

  /// Insert/replace a block. Evicts LRU if at capacity.
  void put(std::uint64_t block_index, std::vector<std::byte> data);

  /// Determine which blocks in [first, last] are missing from cache.
  std::vector<std::uint64_t> missing_blocks(std::uint64_t first_block,
                                            std::uint64_t last_block) const;

  /// Bulk insert after a coalesced fetch.
  void put_range(std::uint64_t first_block,
                 const std::byte* data, std::size_t total_bytes);

  /// Invalidate all cached data (e.g., on detected modification).
  void clear();

  const Stats& stats() const { return stats_; }
  std::size_t block_size() const { return block_size_; }

private:
  std::size_t block_size_;
  std::size_t max_blocks_;
  Stats stats_;

  /// LRU list: front = most recently used, back = eviction candidate.
  std::list<CacheBlock> lru_list_;

  /// Fast lookup by block_index → iterator into lru_list_.
  std::unordered_map<std::uint64_t,
                     std::list<CacheBlock>::iterator> index_;

  void evict_one();
  void touch(std::list<CacheBlock>::iterator it);
};

} // namespace bgen::io
```

### 9.2 Read coalescing strategy

When `read_exact(offset, n)` spans multiple cache blocks:

```
Example: block_size=256KiB, read at offset 500KiB, length 100KiB
  → blocks needed: [1, 2]  (block 1: 256K-512K, block 2: 512K-768K)
  → check cache for blocks 1, 2
  → if both missing & gap < coalesce_gap_limit:
      single GET Range: bytes=262144-786431
      split response into block 1 and block 2, store both
  → if only block 2 missing:
      GET Range: bytes=524288-786431
      store block 2
  → assemble output from cached blocks, copy relevant slice
```

### 9.3 Prefetch heuristic

For sequential variant iteration (the common case):

```cpp
// Inside RangeReaderSource
struct PrefetchState {
  std::uint64_t last_block_accessed = UINT64_MAX;
  std::uint32_t sequential_count = 0;    // consecutive forward reads
  static constexpr uint32_t threshold = 3;  // enable prefetch after N sequential reads
  std::uint32_t prefetch_ahead = 2;      // number of blocks to prefetch
};
```

After detecting sequential access pattern (3+ forward reads), automatically
request `prefetch_ahead` additional blocks beyond the current read.
This amortizes latency for the common `parse_all_variants()` scan.

### 9.4 Integration with existing seek model

Current code does:
```cpp
handle->seekg(offset);
handle->read(buf, n);
```

The new `IRandomAccessSource::as_istream()` returns a custom `std::istream`
backed by a `std::streambuf` subclass that translates `seekg`/`read` into
`read_exact()` calls:

```cpp
// src/io/source_streambuf.h
namespace bgen::io {

/// Custom streambuf that delegates to IRandomAccessSource.
/// Allows existing Header/Variant parsing code to work unchanged.
class SourceStreambuf final : public std::streambuf {
public:
  explicit SourceStreambuf(std::shared_ptr<IRandomAccessSource> source,
                          std::size_t internal_buf_size = 8192);

protected:
  // seekoff/seekpos → update position
  pos_type seekoff(off_type off, std::ios_base::seekdir dir,
                   std::ios_base::openmode which) override;
  pos_type seekpos(pos_type pos,
                   std::ios_base::openmode which) override;

  // underflow → fetch next chunk via read_exact()
  int_type underflow() override;

private:
  std::shared_ptr<IRandomAccessSource> source_;
  std::vector<char> buffer_;
  std::uint64_t file_pos_ = 0;      // current logical position
  std::uint64_t buf_start_ = 0;     // file offset of buffer_[0]
};

} // namespace bgen::io
```

## 10) Threading model

Phase 1 (minimal risk): reader instance is single-threaded and internally synchronized only where required.

Phase 2:

- optional shared `IHttpClient` connection pool
- optional prefetch worker for sequential variant iteration
- keep cache thread-safe with `std::mutex` or sharded locks

Avoid global mutable state except cURL init singleton.

## 11) Error handling and API exposure

### 11.1 Error taxonomy

```cpp
// src/common/error.h
namespace bgen {

enum class ErrorCategory {
  IO,               // local file I/O failures
  Network,          // connection refused, DNS failure, reset
  Timeout,          // connect or request timeout exceeded
  Auth,             // credential retrieval failure, signature mismatch (403)
  NotFound,         // object/bucket does not exist (404)
  InvalidResponse,  // unexpected HTTP status or malformed response
  Configuration,    // invalid config (bad URI, missing required fields)
  Internal,         // programming error / assertion
};

class BgenError : public std::runtime_error {
public:
  BgenError(ErrorCategory cat, const std::string& msg,
            long http_status = 0, std::string request_id = "");

  ErrorCategory category() const { return category_; }
  long http_status() const { return http_status_; }
  const std::string& request_id() const { return request_id_; }

  /// Convenience: is this a retryable error?
  bool is_retryable() const;

private:
  ErrorCategory category_;
  long http_status_;
  std::string request_id_;  // AWS x-amz-request-id for debugging
};

} // namespace bgen
```

### 11.2 Error mapping from HTTP responses

```
HTTP 400 → ErrorCategory::InvalidResponse
HTTP 401 → ErrorCategory::Auth
HTTP 403 → ErrorCategory::Auth (trigger credential refresh)
HTTP 404 → ErrorCategory::NotFound
HTTP 429 → ErrorCategory::Network (retryable, throttled)
HTTP 500 → ErrorCategory::Network (retryable)
HTTP 502 → ErrorCategory::Network (retryable)
HTTP 503 → ErrorCategory::Network (retryable)
cURL CURLE_COULDNT_CONNECT → ErrorCategory::Network
cURL CURLE_OPERATION_TIMEDOUT → ErrorCategory::Timeout
cURL CURLE_SSL_* → ErrorCategory::Network (TLS failure)
```

### 11.3 Python exception mapping (Cython layer)

```python
# In Cython wrapper:
# BgenError(Auth) → raises bgen.S3AuthError (subclass of PermissionError)
# BgenError(NotFound) → raises FileNotFoundError
# BgenError(Network) → raises ConnectionError
# BgenError(Timeout) → raises TimeoutError
# BgenError(IO) → raises IOError
# BgenError(Configuration) → raises ValueError
```

- Keep Python API ergonomic:
  - existing path argument accepts `s3://bucket/key`
  - optional config object/dict for region/profile/endpoint/cache/retry

## 12) Integration points in existing code

### 12.1 Session construction (end-to-end wiring)

When `CppBgenReader` receives an S3 URI, the following object graph is constructed:

```
BgenReader("s3://bucket/data.bgen", config={...})
  │
  ├─ ParsedUri::parse("s3://bucket/data.bgen")
  │    → Scheme::S3, bucket="bucket", key="data.bgen"
  │
  ├─ S3Config resolved from user config + env
  │
  ├─ CurlHttpClient(tls_config, conn_config)
  │    └─ owns CURL handle pool (1–4 handles)
  │    └─ manages TCP keepalive, HTTP/2
  │
  ├─ make_default_credentials_chain(http_client, config)
  │    └─ CachingCredentialsProvider
  │         └─ CredentialsProviderChain [
  │              StaticCredentialsProvider,
  │              EnvironmentCredentialsProvider,
  │              SharedFileCredentialsProvider,
  │              EcsCredentialsProvider,
  │              ImdsCredentialsProvider
  │            ]
  │
  ├─ S3ObjectStore(http_client, creds_provider, config)
  │    └─ owns SigV4Signer(region, "s3")
  │    └─ owns RetryPolicy(retry_config)
  │
  └─ RangeReaderSource(s3_object_store, {bucket, key}, range_opts)
       └─ owns BlockCache(block_size=256K, max_blocks=64)
       └─ calls HEAD to get object size
       └─ provides IRandomAccessSource interface to reader
```

### 12.2 Source factory

```cpp
// src/io/source_factory.h
namespace bgen::io {

/// Constructs the appropriate IRandomAccessSource based on URI scheme.
class SourceFactory {
public:
  /// Build source for the given URI string.
  /// For S3 URIs, uses the provided S3Config; for local files, ignores it.
  static std::shared_ptr<IRandomAccessSource> create(
      const std::string& uri,
      const s3::S3Config& config = {});

private:
  static std::shared_ptr<IRandomAccessSource> create_local(const std::string& path);
  static std::shared_ptr<IRandomAccessSource> create_stdin();
  static std::shared_ptr<IRandomAccessSource> create_s3(
      const ParsedUri& parsed, const s3::S3Config& config);
};

} // namespace bgen::io
```

### 12.3 Modified CppBgenReader constructor

```cpp
// src/reader.cpp (modified)
CppBgenReader::CppBgenReader(std::string path, std::string sample_path,
                             bool delay_parsing, s3::S3Config s3_config) {
  // Parse URI and create appropriate source
  source_ = io::SourceFactory::create(path, s3_config);
  
  // Get istream adapter for legacy parsing code
  handle = source_->as_istream().release();
  owns_handle_ = true;

  if (handle->fail()) {
    throw std::invalid_argument("error reading from '" + path + "'");
  }

  // Determine stdin mode
  auto parsed = io::ParsedUri::parse(path);
  is_stdin = (parsed.scheme == io::ParsedUri::Scheme::Stdin);

  header = Header(handle);
  if (header.has_sample_ids) {
    samples = Samples(handle, header.nsamples);
  } else if (sample_path.size() > 0) {
    samples = Samples(sample_path, header.nsamples);
  } else {
    samples = Samples(header.nsamples);
  }

  offset = header.offset + 4;
  if (!delay_parsing) {
    parse_all_variants();
  }
}
```

### 12.4 Component ownership and lifetime

```
┌─────────────────────────────────────────────────────────────────┐
│ CppBgenReader                                                    │
│   owns: shared_ptr<IRandomAccessSource> source_                  │
│                                                                  │
│   ┌────────────────────────────────────────────────────────┐    │
│   │ RangeReaderSource                                       │    │
│   │   owns: unique_ptr<BlockCache>                          │    │
│   │   shares: shared_ptr<IObjectStore>  ──────────────┐    │    │
│   └────────────────────────────────────────────────────│────┘    │
│                                                        │         │
│   ┌────────────────────────────────────────────────────▼────┐    │
│   │ S3ObjectStore                                           │    │
│   │   owns: SigV4Signer, RetryPolicy                       │    │
│   │   shares: shared_ptr<IHttpClient> ────────────────┐    │    │
│   │   shares: shared_ptr<ICredentialsProvider> ───┐   │    │    │
│   └───────────────────────────────────────────────│───│────┘    │
│                                                   │   │          │
│   ┌───────────────────────────────────────────────▼───│────┐    │
│   │ CachingCredentialsProvider                        │    │    │
│   │   owns: unique_ptr<CredentialsProviderChain>      │    │    │
│   └───────────────────────────────────────────────────│────┘    │
│                                                       │          │
│   ┌───────────────────────────────────────────────────▼────┐    │
│   │ CurlHttpClient                                         │    │
│   │   owns: CURL handle pool (Impl via PIMPL)              │    │
│   │   owns: TLS context state                              │    │
│   └────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

Key lifetime rules:
- `CurlHttpClient` can outlive individual requests (connection reuse).
- `CachingCredentialsProvider` can be shared across multiple readers hitting the same account.
- `BlockCache` is per-reader (no cross-reader cache sharing by default).
- All destructors clean up gracefully; `CurlHttpClient::shutdown()` drains in-flight work.

### 12.5 `Header`, `Variant`, `Samples` — no changes needed

These classes consume `std::istream*` which is now backed by `SourceStreambuf`.
No S3-specific code or dependencies leak into parsing logic.

### 12.6 Cython wrapper changes

```python
# src/bgen/reader.pyx (additions)
cdef class BgenReader:
    def __init__(self, path, sample_path='', delay_parsing=False, s3_config=None):
        # s3_config is a dict: {region, endpoint, profile, ...}
        # Convert to C++ S3Config struct via Cython conversion
        ...
```

## 13) Testing strategy

- Unit tests (pure C++):
  - URI parser
  - SigV4 canonicalization/signing known vectors
  - credential provider precedence
  - retry policy decisions
  - range cache behavior
- Integration tests:
  - mocked `IHttpClient` for deterministic S3 responses
  - optional live tests gated by env vars against MinIO/AWS test bucket
- Python-level tests:
  - `BgenReader("s3://...")` parity with local for key workflows
  - range-heavy random-access scenarios

## 14) Caching recommendations

### 14.1 In-memory LRU block cache

- Default: per-reader instance, 64 blocks × 256 KiB = 16 MiB max footprint.
- Configurable: `range_cache_blocks=0` disables caching entirely.
- LRU eviction: `std::list` for O(1) move-to-front, `unordered_map` for O(1) lookup.

### 14.2 Cache coherency

- No built-in invalidation (S3 objects are immutable once written).
- If ETag mismatch detected on a range GET (HTTP 412), clear cache and re-HEAD.
- Version-aware: if `ObjectRef.version_id` is set, cache is implicitly coherent.

### 14.3 Cache warming strategy

For `parse_all_variants()` which scans the entire file sequentially:

```cpp
/// Pre-fetch first N blocks on open to reduce initial latency.
struct CacheWarmConfig {
  std::size_t warmup_blocks = 4;       // fetch first 1 MiB on open
  bool warmup_on_construct = true;
};
```

### 14.4 Future: shared cross-reader cache

```cpp
/// Optional shared cache that multiple reader instances can reference.
/// Keyed by (bucket, key, version_id, block_index).
class SharedBlockCache {
public:
  using CacheKey = std::tuple<std::string, std::string, std::string, std::uint64_t>;
  
  SharedBlockCache(std::size_t max_total_bytes);

  const CacheBlock* get(const CacheKey& key);
  void put(const CacheKey& key, std::vector<std::byte> data);

private:
  std::size_t max_bytes_;
  std::size_t current_bytes_ = 0;
  std::mutex mu_;
  // ... LRU implementation keyed by full tuple
};
```

This allows multiple `BgenReader` instances reading from the same S3 bucket
to share cached blocks without redundant fetches.

### 14.5 Future: disk-backed cache

```cpp
/// Interface for optional on-disk caching layer.
class IDiskCache {
public:
  virtual ~IDiskCache() = default;
  virtual std::optional<std::vector<std::byte>> get(
      const std::string& key, std::uint64_t block_index) = 0;
  virtual void put(const std::string& key, std::uint64_t block_index,
                   std::span<const std::byte> data) = 0;
  virtual void evict(const std::string& key) = 0;
};
```

Could be backed by SQLite or simple file-per-block layout.
Integrates below BlockCache: miss in memory → check disk → fetch from S3.

## 15) Portability and build rollout

- Keep S3 feature optional at build time first (`BGEN_ENABLE_S3=OFF` default)
- Ensure clear compile-time error when S3 URI is used without S3-enabled build
- Add CI matrix later for Linux/macOS with libcurl+OpenSSL

## 16) Incremental implementation plan

1. Add interfaces + local source adapter (no behavior change).
2. Refactor reader internals to use `IRandomAccessSource`.
3. Add HTTP abstraction + curl backend.
4. Add credentials + SigV4.
5. Add `S3ObjectStore` + `RangeReaderSource` + retries/cache.
6. Wire Cython/Python config.
7. Add tests and docs; enable feature flag by default once stable.

This path keeps changes controlled and testable while delivering a clean long-term architecture.
