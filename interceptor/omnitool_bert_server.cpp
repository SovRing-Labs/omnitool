/*
 * omnitool_bert_server.cpp - Native C++ BERT Preflight Server
 * 
 * Unix Domain Socket IPC for sub-millisecond communication
 * ONNX Runtime C++ execution (1-3ms latency)
 * Locks model into RAM with mlock for zero-swap stability
 * 
 * Build: g++ -O3 -std=c++17 -I/usr/include/onnxruntime omnitool_bert_server.cpp -lonnxruntime -lpthread -o omnitool_bert_server
 * 
 * Protocol: JSON request/response over Unix socket
 *   Request:  {"text": "read the config file"} or {"payload": {...}}
 *   Response: {"intent": "read", "intent_confidence": 0.85, "safety": "safe", "safety_confidence": 0.92, "anomaly_score": 0.15, "is_anomaly": false}
 */

#include <iostream>
#include <vector>
#include <string>
#include <cstring>
#include <memory>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <queue>
#include <atomic>
#include <chrono>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <signal.h>
#include <climits>
#include <onnxruntime_cxx_api.h>
#include <nlohmann/json.hpp>

#define SOCKET_PATH "/tmp/omnitool_bert.sock"
#define MAX_SEQ_LEN 128
#define EMBEDDING_DIM 384
#define MAX_CONNECTIONS 128

using json = nlohmann::json;

struct ClassifierConfig {
    float confidence_threshold = 0.12f;
    float anomaly_threshold = 0.80f;
    int num_threads = 2;
};

struct PrototypeEntry {
    std::string intent;
    std::vector<float> embedding;
};

class BertPreflightServer {
private:
    Ort::Env env_;
    Ort::SessionOptions session_options_;
    std::unique_ptr<Ort::Session> session_;
    Ort::MemoryInfo memory_info_;
    ClassifierConfig config_;
    
    std::vector<PrototypeEntry> intent_prototypes_;
    std::vector<PrototypeEntry> safety_prototypes_;
    
    int server_fd_ = -1;
    std::atomic<bool> running_{true};
    std::thread worker_thread_;
    std::mutex queue_mutex_;
    std::condition_variable queue_cv_;
    std::queue<int> client_queue_;
    
    std::atomic<uint64_t> total_requests_{0};
    std::atomic<uint64_t> total_latency_us_{0};
    std::atomic<uint64_t> errors_{0};

public:
    BertPreflightServer(const std::string& model_path, const ClassifierConfig& config = {})
        : env_(ORT_LOGGING_LEVEL_WARNING, "BertPreflightServer"),
          session_options_(),
          memory_info_(Ort::MemoryInfo::CreateCpu(OrtArenaAllocator, OrtMemTypeDefault)),
          config_(config) {
        
        session_options_.SetIntraOpNumThreads(config_.num_threads);
        session_options_.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);
        session_options_.EnableMemPattern();
        session_options_.EnableCpuMemArena();
        
        session_ = std::make_unique<Ort::Session>(env_, model_path.c_str(), session_options_);
        std::cout << "[bert-server] Model loaded: " << model_path << std::endl;
        
        init_prototypes();
        setup_socket();
    }
    
    ~BertPreflightServer() {
        stop();
        if (server_fd_ >= 0) close(server_fd_);
        unlink(SOCKET_PATH);
    }
    
    void run() {
        std::cout << "[bert-server] Listening on Unix Socket: " << SOCKET_PATH << std::endl;
        std::cout << "[bert-server] Threads: " << config_.num_threads 
                  << ", Confidence: " << config_.confidence_threshold
                  << ", Anomaly: " << config_.anomaly_threshold << std::endl;
        
        worker_thread_ = std::thread(&BertPreflightServer::worker_loop, this);
        
        while (running_) {
            int client_fd = accept(server_fd_, nullptr, nullptr);
            if (client_fd < 0) {
                if (errno == EINTR || errno == EAGAIN) continue;
                std::cerr << "[bert-server] Accept error: " << strerror(errno) << std::endl;
                break;
            }
            
            int flags = fcntl(client_fd, F_GETFL, 0);
            fcntl(client_fd, F_SETFL, flags | O_NONBLOCK);
            
            {
                std::lock_guard<std::mutex> lock(queue_mutex_);
                client_queue_.push(client_fd);
            }
            queue_cv_.notify_one();
        }
        
        worker_thread_.join();
    }
    
    void stop() {
        running_ = false;
        queue_cv_.notify_all();
        if (worker_thread_.joinable()) worker_thread_.join();
    }
    
    void print_stats() const {
        uint64_t req = total_requests_.load();
        uint64_t lat = total_latency_us_.load();
        uint64_t err = errors_.load();
        if (req > 0) {
            std::cout << "[bert-server] Stats: requests=" << req 
                      << ", avg_latency_ms=" << (lat / req) / 1000.0
                      << ", errors=" << err << std::endl;
        }
    }

private:
    void setup_socket() {
        unlink(SOCKET_PATH);
        server_fd_ = socket(AF_UNIX, SOCK_STREAM, 0);
        if (server_fd_ < 0) {
            throw std::runtime_error("Failed to create socket: " + std::string(strerror(errno)));
        }
        
        struct sockaddr_un addr{};
        addr.sun_family = AF_UNIX;
        strncpy(addr.sun_path, SOCKET_PATH, sizeof(addr.sun_path) - 1);
        
        if (bind(server_fd_, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
            throw std::runtime_error("Failed to bind socket: " + std::string(strerror(errno)));
        }
        
        if (listen(server_fd_, MAX_CONNECTIONS) < 0) {
            throw std::runtime_error("Failed to listen: " + std::string(strerror(errno)));
        }
    }
    
    void init_prototypes() {
        std::cout << "[bert-server] Prototypes will be loaded on first request (lazy init)" << std::endl;
    }
    
    void worker_loop() {
        while (running_) {
            int client_fd = -1;
            {
                std::unique_lock<std::mutex> lock(queue_mutex_);
                queue_cv_.wait(lock, [this] { return !client_queue_.empty() || !running_; });
                if (!running_ && client_queue_.empty()) break;
                if (!client_queue_.empty()) {
                    client_fd = client_queue_.front();
                    client_queue_.pop();
                }
            }
            
            if (client_fd >= 0) {
                handle_client(client_fd);
                close(client_fd);
            }
        }
    }
    
    void handle_client(int client_fd) {
        struct pollfd pfd{};
        pfd.fd = client_fd;
        pfd.events = POLLIN;
        int pr = poll(&pfd, 1, 1000);
        if (pr <= 0) return;

        char buffer[8192];
        ssize_t bytes = recv(client_fd, buffer, sizeof(buffer) - 1, 0);
        if (bytes <= 0) return;
        
        buffer[bytes] = '\0';
        
        auto start = std::chrono::high_resolution_clock::now();
        
        try {
            json request = json::parse(buffer);
            json response;
            
            if (request.contains("text")) {
                response = classify_text(request["text"].get<std::string>());
            } else if (request.contains("payload")) {
                response = classify_payload(request["payload"]);
            } else if (request.contains("health")) {
                response = health_check();
            } else {
                response = {{"error", "Invalid request: need 'text' or 'payload' or 'health'"}};
            }
            
            std::string resp_str = response.dump();
            send(client_fd, resp_str.c_str(), resp_str.size(), MSG_NOSIGNAL);
            
        } catch (const std::exception& e) {
            json error_resp = {{"error", e.what()}};
            std::string resp_str = error_resp.dump();
            send(client_fd, resp_str.c_str(), resp_str.size(), MSG_NOSIGNAL);
            errors_++;
        }
        
        auto end = std::chrono::high_resolution_clock::now();
        auto latency = std::chrono::duration_cast<std::chrono::microseconds>(end - start).count();
        total_requests_++;
        total_latency_us_ += latency;
    }
    
    // NOT YET IMPLEMENTED: this used to return a hardcoded constant regardless
    // of `text`, which meant it silently classified rm -rf, curl exfil, and
    // DROP TABLE identically to a benign read as "safe" (verified live,
    // 2026-09-22). No real inference is run against the loaded ONNX session
    // here despite the model being resident in memory. Returning an explicit
    // error is deliberate: fleet-exec's caller (bin/fleet-exec) treats an
    // "error" key as a failed tier and falls through to the real classifier
    // (omnitool_intent_classifier.py, tier 2/3 — actual cosine-similarity
    // prototype matching). A fabricated "safe" verdict is worse than no
    // verdict at all. Real fix: WordPiece tokenize `text` (need the
    // all-MiniLM-L6-v2 vocab), run session_.Run() on the ONNX session
    // already constructed in this class, mean-pool + L2-normalize the
    // output, then cosine-compare against precomputed prototype embeddings
    // (same prototypes as omnitool_intent_classifier.py's few-shot lists) —
    // not done here to avoid shipping a second, unverified classifier
    // implementation next to the working Python one.
    json classify_text(const std::string& text) {
        return {
            {"error", "not_implemented: fast-path inference not built, see comment above classify_text()"}
        };
    }
    
    json classify_payload(const json& payload) {
        std::string combined_text;
        if (payload.contains("operations") && payload["operations"].is_array()) {
            for (const auto& op : payload["operations"]) {
                if (!combined_text.empty()) combined_text += " ; ";
                if (op.contains("type")) combined_text += op["type"].get<std::string>();
                if (op.contains("command")) combined_text += " " + op["command"].get<std::string>();
                if (op.contains("path")) combined_text += " " + op["path"].get<std::string>();
                if (op.contains("database")) combined_text += " " + op["database"].get<std::string>();
            }
        }
        
        return classify_text(combined_text.empty() ? "unknown" : combined_text);
    }
    
    json health_check() {
        return {
            {"status", "ok"},
            {"model", "all-MiniLM-L6-v2"},
            {"uptime_requests", total_requests_.load()},
            {"avg_latency_ms", total_requests_ > 0 ? (total_latency_us_.load() / total_requests_.load()) / 1000.0 : 0.0},
            {"errors", errors_.load()}
        };
    }
};

static std::unique_ptr<BertPreflightServer> g_server;

void signal_handler(int sig) {
    std::cout << "\n[bert-server] Shutdown signal received" << std::endl;
    if (g_server) {
        g_server->stop();
        g_server->print_stats();
    }
    exit(0);
}

// Resolve the model path relative to this executable's own location
// (<project>/bin/omnitool_bert_server -> <project>/bert_onnx/bert_interceptor.onnx)
// instead of the launching process's CWD, so the server works regardless of
// where it's started from.
std::string default_model_path() {
    char exe_path[PATH_MAX];
    ssize_t len = readlink("/proc/self/exe", exe_path, sizeof(exe_path) - 1);
    if (len != -1) {
        exe_path[len] = '\0';
        std::string path(exe_path);
        size_t bin_dir_end = path.find_last_of('/');
        if (bin_dir_end != std::string::npos) {
            std::string bin_dir = path.substr(0, bin_dir_end);
            size_t project_dir_end = bin_dir.find_last_of('/');
            if (project_dir_end != std::string::npos) {
                std::string project_dir = bin_dir.substr(0, project_dir_end);
                return project_dir + "/bert_onnx/bert_interceptor.onnx";
            }
        }
    }
    return "bin/bert_onnx/bert_interceptor.onnx";  // last-resort fallback
}

int main(int argc, char* argv[]) {
    std::string model_path = default_model_path();
    int threads = 2;
    
    if (argc > 1) model_path = argv[1];
    if (argc > 2) threads = std::stoi(argv[2]);
    
    if (access(model_path.c_str(), F_OK) != 0) {
        std::cerr << "[bert-server] Model not found: " << model_path << std::endl;
        return 1;
    }
    
    if (mlockall(MCL_CURRENT | MCL_FUTURE) != 0) {
        std::cerr << "[bert-server] Warning: mlockall failed: " << strerror(errno) << std::endl;
    } else {
        std::cout << "[bert-server] Memory locked (mlockall)" << std::endl;
    }
    
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);
    
    ClassifierConfig config;
    config.num_threads = threads;
    
    try {
        g_server = std::make_unique<BertPreflightServer>(model_path, config);
        g_server->run();
    } catch (const std::exception& e) {
        std::cerr << "[bert-server] Fatal error: " << e.what() << std::endl;
        return 1;
    }
    
    return 0;
}