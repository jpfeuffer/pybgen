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
class IRandomAccessSource {
public:
  virtual ~IRandomAccessSource() = default;
  virtual std::uint64_t size() const = 0;
  virtual void read_exact(std::uint64_t offset, std::span<std::byte> out) = 0;
  virtual bool supports_zero_copy_window() const { return false; }
};
```

`CppBgenReader` stores `std::shared_ptr<IRandomAccessSource>` instead of owning `std::ifstream` directly.

- `LocalFileSource`: uses `std::ifstream` + seek/read.
- `RangeReaderSource`: uses S3 range GET with block cache.

### 3.2 HTTP abstraction

```cpp
struct HttpRequest {
  std::string method;
  std::string url;
  std::vector<std::pair<std::string, std::string>> headers;
  std::optional<std::pair<std::uint64_t, std::uint64_t>> byte_range;
  std::string body;
  std::chrono::milliseconds timeout;
};

struct HttpResponse {
  long status;
  std::vector<std::pair<std::string, std::string>> headers;
  std::string body;
};

class IHttpClient {
public:
  virtual ~IHttpClient() = default;
  virtual HttpResponse perform(const HttpRequest&) = 0;
};
```

`CurlHttpClient` is the only class aware of libcurl handles/options.

### 3.3 Object store abstraction (future extensibility)

```cpp
class IObjectStore {
public:
  virtual ~IObjectStore() = default;
  virtual std::uint64_t head_size(const ObjectRef&) = 0;
  virtual std::string get_range(const ObjectRef&, std::uint64_t begin, std::uint64_t end_inclusive) = 0;
};
```

`S3ObjectStore` implements `IObjectStore`; future `GcsObjectStore`/`AzureBlobStore` can be added with no reader changes.

## 4) Configuration model

`S3Config` (plain value struct):

- `region`, `endpoint_override`, `use_https` (default true), `verify_tls` (default true)
- `ca_bundle_path`, `connect_timeout`, `request_timeout`
- `max_retries`, `retry_base_delay`, `retry_max_delay`
- `range_block_size` (e.g. 256 KiB), `range_cache_blocks`
- `path_style_addressing` toggle

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

## 10) Threading model

Phase 1 (minimal risk): reader instance is single-threaded and internally synchronized only where required.

Phase 2:

- optional shared `IHttpClient` connection pool
- optional prefetch worker for sequential variant iteration
- keep cache thread-safe with `std::mutex` or sharded locks

Avoid global mutable state except cURL init singleton.

## 11) Error handling and API exposure

- Introduce `BgenError` with category + message + optional status code.
- Internal C++ throws `BgenError`; Cython layer converts to Python exceptions.
- Keep Python API ergonomic:
  - existing path argument accepts `s3://bucket/key`
  - optional config object/dict for region/profile/endpoint/cache/retry

## 12) Integration points in existing code

1. `CppBgenReader` constructor:
   - parse input URI
   - build `IRandomAccessSource` using factory
   - no direct `ifstream` assumptions in downstream parsing paths
2. `Header`, `Variant`, `Samples`:
   - consume byte reads via source-backed stream adapter
   - no S3-specific code
3. Cython wrapper:
   - expose optional S3 config argument
   - preserve current behavior for local paths/stdin

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

- default in-memory LRU block cache per reader instance
- configurable size; disable with `range_cache_blocks=0`
- optional future disk cache interface (separate module) without affecting parser code

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
