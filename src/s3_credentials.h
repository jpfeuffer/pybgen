#ifndef BGEN_S3_CREDENTIALS_H_
#define BGEN_S3_CREDENTIALS_H_

#include <string>
#include <fstream>
#include <cstdlib>
#include <memory>
#include <stdexcept>
#include <chrono>
#include <sstream>

namespace bgen {
namespace s3 {

/// Holds a set of AWS credentials
struct Credentials {
    std::string access_key;
    std::string secret_key;
    std::string session_token;  // optional, for temporary credentials
    
    bool is_valid() const {
        return !access_key.empty() && !secret_key.empty();
    }
};

/// Abstract credential provider interface
class CredentialProvider {
public:
    virtual ~CredentialProvider() = default;
    virtual Credentials get_credentials() = 0;
    virtual std::string name() const = 0;
};

/// Loads credentials from environment variables
/// AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN
class EnvCredentialProvider : public CredentialProvider {
public:
    Credentials get_credentials() override {
        Credentials creds;
        const char* ak = std::getenv("AWS_ACCESS_KEY_ID");
        const char* sk = std::getenv("AWS_SECRET_ACCESS_KEY");
        const char* st = std::getenv("AWS_SESSION_TOKEN");
        if (ak) creds.access_key = ak;
        if (sk) creds.secret_key = sk;
        if (st) creds.session_token = st;
        return creds;
    }
    std::string name() const override { return "EnvCredentialProvider"; }
};

/// Loads credentials from ~/.aws/credentials file
class FileCredentialProvider : public CredentialProvider {
    std::string profile_;
public:
    explicit FileCredentialProvider(const std::string& profile = "default")
        : profile_(profile) {}
    
    Credentials get_credentials() override {
        std::string path = get_credentials_path();
        std::ifstream file(path);
        if (!file.is_open()) {
            return Credentials{};
        }
        
        Credentials creds;
        std::string line;
        bool in_profile = false;
        std::string target_section = "[" + profile_ + "]";
        
        while (std::getline(file, line)) {
            // Trim whitespace
            size_t start = line.find_first_not_of(" \t\r\n");
            if (start == std::string::npos) continue;
            line = line.substr(start);
            
            if (line.empty() || line[0] == '#') continue;
            
            if (line[0] == '[') {
                in_profile = (line.find(target_section) == 0);
                continue;
            }
            
            if (in_profile) {
                size_t eq = line.find('=');
                if (eq != std::string::npos) {
                    std::string key = line.substr(0, eq);
                    std::string value = line.substr(eq + 1);
                    // trim
                    key.erase(key.find_last_not_of(" \t") + 1);
                    value.erase(0, value.find_first_not_of(" \t"));
                    
                    if (key == "aws_access_key_id") creds.access_key = value;
                    else if (key == "aws_secret_access_key") creds.secret_key = value;
                    else if (key == "aws_session_token") creds.session_token = value;
                }
            }
        }
        return creds;
    }
    
    std::string name() const override { return "FileCredentialProvider"; }
    
private:
    std::string get_credentials_path() {
        const char* path = std::getenv("AWS_SHARED_CREDENTIALS_FILE");
        if (path) return path;
        
        const char* home = std::getenv("HOME");
        if (!home) home = std::getenv("USERPROFILE");
        if (!home) return "";
        
        return std::string(home) + "/.aws/credentials";
    }
};

/// Anonymous credential provider for public buckets (no-sign-request)
class AnonymousCredentialProvider : public CredentialProvider {
public:
    Credentials get_credentials() override {
        // Return empty credentials; requests will not be signed
        return Credentials{};
    }
    std::string name() const override { return "AnonymousCredentialProvider"; }
    bool is_anonymous() const { return true; }
};

/// Explicit credential provider - holds credentials passed directly
class ExplicitCredentialProvider : public CredentialProvider {
    Credentials creds_;
public:
    ExplicitCredentialProvider(const std::string& access_key,
                              const std::string& secret_key,
                              const std::string& session_token = "")
        : creds_{access_key, secret_key, session_token} {}
    
    Credentials get_credentials() override { return creds_; }
    std::string name() const override { return "ExplicitCredentialProvider"; }
};

/// Credential provider chain - tries providers in order until one succeeds
class CredentialChain : public CredentialProvider {
    std::vector<std::unique_ptr<CredentialProvider>> providers_;
public:
    CredentialChain() {
        // Default chain: env vars first, then credentials file
        const char* profile = std::getenv("AWS_PROFILE");
        std::string prof = profile ? profile : "default";
        
        providers_.push_back(std::unique_ptr<CredentialProvider>(new EnvCredentialProvider()));
        providers_.push_back(std::unique_ptr<CredentialProvider>(new FileCredentialProvider(prof)));
    }
    
    explicit CredentialChain(const std::string& profile) {
        // Chain with explicit profile selection
        providers_.push_back(std::unique_ptr<CredentialProvider>(new EnvCredentialProvider()));
        providers_.push_back(std::unique_ptr<CredentialProvider>(new FileCredentialProvider(profile)));
    }
    
    void add_provider(std::unique_ptr<CredentialProvider> provider) {
        providers_.push_back(std::move(provider));
    }
    
    Credentials get_credentials() override {
        for (auto& provider : providers_) {
            Credentials creds = provider->get_credentials();
            if (creds.is_valid()) {
                return creds;
            }
        }
        return Credentials{};
    }
    
    std::string name() const override { return "CredentialChain"; }
};

} // namespace s3
} // namespace bgen

#endif  // BGEN_S3_CREDENTIALS_H_
