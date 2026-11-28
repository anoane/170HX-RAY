/* SPDX-FileCopyrightText: 2026 Fabiano Anemone <fabiano.anemone@gmail.com> */
/* SPDX-License-Identifier: AGPL-3.0-or-later */
/*
 * 170HX-XRAY CUDA diagnostics: non-persistent bandwidth and P2P measurements.
 *
 * This program never opens BAR0 or a fuse controller.  It uses documented
 * CUDA/NVML APIs, emits one JSON document, and leaves device selection to the
 * orchestrator.  Build with CUDA 11+ for sm_80.
 */
#include <cuda_runtime.h>
#include <cuda_bf16.h>
#include <cuda_fp16.h>
#include <nvml.h>

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <climits>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

#define CUDA_OK(call) do {                                                   \
    cudaError_t xray_e = (call);                                              \
    if (xray_e != cudaSuccess) {                                              \
        std::fprintf(stderr, "CUDA failure %s:%d: %s\n", __FILE__, __LINE__, \
                     cudaGetErrorString(xray_e));                             \
        return 2;                                                            \
    }                                                                        \
} while (0)

static std::string json_escape(const char *text) {
    std::string out;
    for (const unsigned char *p = reinterpret_cast<const unsigned char *>(text);
         *p; ++p) {
        switch (*p) {
        case '\\': out += "\\\\"; break;
        case '"': out += "\\\""; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
            if (*p < 0x20) {
                char buf[8];
                std::snprintf(buf, sizeof(buf), "\\u%04x", *p);
                out += buf;
            } else out += static_cast<char>(*p);
        }
    }
    return out;
}

struct TelemetrySample {
    double seconds;
    unsigned int power_mw;
    unsigned int sm_mhz;
    unsigned int memory_mhz;
    unsigned int temperature_c;
    unsigned int gpu_util;
    unsigned int memory_util;
    bool power_valid;
    bool sm_clock_valid;
    bool memory_clock_valid;
    bool temperature_valid;
    bool utilization_valid;
};

static bool query_gpu_temperature(nvmlDevice_t device, unsigned int *value) {
#if defined(NVML_API_VERSION) && NVML_API_VERSION >= 13
    nvmlTemperature_t temperature{};
    temperature.version = nvmlTemperature_v1;
    temperature.sensorType = NVML_TEMPERATURE_GPU;
    if (nvmlDeviceGetTemperatureV(device, &temperature) != NVML_SUCCESS ||
        temperature.temperature < 0) {
        return false;
    }
    *value = static_cast<unsigned int>(temperature.temperature);
    return true;
#else
    return nvmlDeviceGetTemperature(device, NVML_TEMPERATURE_GPU, value)
           == NVML_SUCCESS;
#endif
}

class NvmlSampler {
public:
    explicit NvmlSampler(int cuda_device)
        : ready_(false), running_(false), status_("not_initialized") {
        char pci[32] = {0};
        if (cudaDeviceGetPCIBusId(pci, sizeof(pci), cuda_device) != cudaSuccess) {
            status_ = "cuda_pci_lookup_failed";
            return;
        }
        if (nvmlInit_v2() != NVML_SUCCESS) {
            status_ = "nvml_init_failed";
            return;
        }
        initialized_ = true;
        if (nvmlDeviceGetHandleByPciBusId_v2(pci, &handle_) != NVML_SUCCESS) {
            status_ = "nvml_handle_lookup_failed";
            return;
        }
        ready_ = true;
        status_ = "ready";
    }

    ~NvmlSampler() {
        stop();
        if (initialized_) nvmlShutdown();
    }

    void start() {
        if (!ready_ || running_) return;
        samples_.clear();
        start_ = std::chrono::steady_clock::now();
        samples_.push_back(query_sample());
        running_ = true;
        worker_ = std::thread([this]() {
            while (running_) {
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
                if (running_) samples_.push_back(query_sample());
            }
        });
    }

    void stop() {
        if (!running_) return;
        running_ = false;
        if (worker_.joinable()) worker_.join();
    }

    bool ready() const { return ready_; }
    const std::string& status() const { return status_; }
    const std::vector<TelemetrySample>& samples() const { return samples_; }

private:
    TelemetrySample query_sample() const {
        TelemetrySample sample{};
        auto now = std::chrono::steady_clock::now();
        sample.seconds = std::chrono::duration<double>(now - start_).count();
        sample.power_valid = (
            nvmlDeviceGetPowerUsage(handle_, &sample.power_mw) == NVML_SUCCESS
        );
        sample.sm_clock_valid = (
            nvmlDeviceGetClockInfo(handle_, NVML_CLOCK_SM, &sample.sm_mhz)
            == NVML_SUCCESS
        );
        sample.memory_clock_valid = (
            nvmlDeviceGetClockInfo(handle_, NVML_CLOCK_MEM, &sample.memory_mhz)
            == NVML_SUCCESS
        );
        sample.temperature_valid = query_gpu_temperature(
            handle_, &sample.temperature_c
        );
        nvmlUtilization_t util{};
        if (nvmlDeviceGetUtilizationRates(handle_, &util) == NVML_SUCCESS) {
            sample.gpu_util = util.gpu;
            sample.memory_util = util.memory;
            sample.utilization_valid = true;
        }
        return sample;
    }

    bool initialized_ = false;
    bool ready_;
    std::atomic<bool> running_;
    nvmlDevice_t handle_{};
    std::chrono::steady_clock::time_point start_;
    std::thread worker_;
    std::vector<TelemetrySample> samples_;
    std::string status_;
};

struct MetricAggregate {
    double sum = 0.0;
    unsigned int maximum = 0;
    size_t count = 0;

    void add(bool valid, unsigned int value) {
        if (!valid) return;
        sum += value;
        maximum = std::max(maximum, value);
        ++count;
    }
};

static void print_nullable_average(const MetricAggregate& metric,
                                   double scale = 1.0) {
    if (!metric.count) std::printf("null");
    else std::printf("%.6f", metric.sum / metric.count / scale);
}

static void print_nullable_maximum(const MetricAggregate& metric,
                                   double scale = 1.0) {
    if (!metric.count) std::printf("null");
    else std::printf("%.6f", metric.maximum / scale);
}

static void print_telemetry_object(
        bool nvml_available, const std::string& status,
        const std::vector<TelemetrySample>& samples,
        const char *scope) {
    MetricAggregate power, sm_clock, memory_clock, temperature;
    MetricAggregate gpu_utilization, memory_utilization;
    double sampling_span_seconds = 0.0;
    for (const auto& sample : samples) {
        sampling_span_seconds = std::max(sampling_span_seconds, sample.seconds);
        power.add(sample.power_valid, sample.power_mw);
        sm_clock.add(sample.sm_clock_valid, sample.sm_mhz);
        memory_clock.add(sample.memory_clock_valid, sample.memory_mhz);
        temperature.add(sample.temperature_valid, sample.temperature_c);
        gpu_utilization.add(sample.utilization_valid, sample.gpu_util);
        memory_utilization.add(sample.utilization_valid, sample.memory_util);
    }
    std::printf(
        "{\"scope\":\"%s\",\"nvml_available\":%s,\"status\":\"%s\","
        "\"temperature_sensor\":\"nvml-temperature-gpu\","
        "\"sample_count\":%zu,\"sampling_span_seconds\":%.6f,"
        "\"sampling_span_semantics\":\"%s\",",
        json_escape(scope).c_str(), nvml_available ? "true" : "false",
        json_escape(status.c_str()).c_str(), samples.size(),
        sampling_span_seconds,
        std::strcmp(scope, "aggregate-of-per-dtype-bracketing-windows") == 0
            ? "synthetic-sum-of-disjoint-window-sample-spans"
            : "last-relative-sample-offset-within-bracketing-window");
    std::printf(
        "\"valid_sample_counts\":{\"power\":%zu,\"sm_clock\":%zu,"
        "\"memory_clock\":%zu,\"temperature\":%zu,"
        "\"utilization\":%zu},",
        power.count, sm_clock.count, memory_clock.count, temperature.count,
        gpu_utilization.count);
    std::printf(
        "\"query_failure_counts\":{\"power\":%zu,\"sm_clock\":%zu,"
        "\"memory_clock\":%zu,\"temperature\":%zu,"
        "\"utilization\":%zu},",
        samples.size() - power.count, samples.size() - sm_clock.count,
        samples.size() - memory_clock.count,
        samples.size() - temperature.count,
        samples.size() - gpu_utilization.count);
    std::printf("\"power_w_avg\":"); print_nullable_average(power, 1000.0);
    std::printf(",\"power_w_max\":"); print_nullable_maximum(power, 1000.0);
    std::printf(",\"sm_mhz_avg\":"); print_nullable_average(sm_clock);
    std::printf(",\"sm_mhz_max\":"); print_nullable_maximum(sm_clock);
    std::printf(",\"memory_mhz_avg\":"); print_nullable_average(memory_clock);
    std::printf(",\"memory_mhz_max\":"); print_nullable_maximum(memory_clock);
    std::printf(",\"temperature_c_avg\":"); print_nullable_average(temperature);
    std::printf(",\"temperature_c_max\":"); print_nullable_maximum(temperature);
    std::printf(",\"gpu_util_percent_avg\":");
    print_nullable_average(gpu_utilization);
    std::printf(",\"gpu_util_percent_max\":");
    print_nullable_maximum(gpu_utilization);
    std::printf(",\"memory_util_percent_avg\":");
    print_nullable_average(memory_utilization);
    std::printf(",\"memory_util_percent_max\":");
    print_nullable_maximum(memory_utilization);
    std::printf("}");
}

struct DeviceRecord {
    int index;
    cudaDeviceProp properties;
    char pci[32];
    int memory_bus_width;
    int memory_clock_khz;
    int l2_bytes;
};

static int load_device_record(int index, DeviceRecord *record) {
    *record = DeviceRecord{};
    record->index = index;
    CUDA_OK(cudaGetDeviceProperties(&record->properties, index));
    CUDA_OK(cudaDeviceGetPCIBusId(record->pci, sizeof(record->pci), index));
    CUDA_OK(cudaDeviceGetAttribute(
        &record->memory_bus_width, cudaDevAttrGlobalMemoryBusWidth, index));
    CUDA_OK(cudaDeviceGetAttribute(
        &record->memory_clock_khz, cudaDevAttrMemoryClockRate, index));
    CUDA_OK(cudaDeviceGetAttribute(
        &record->l2_bytes, cudaDevAttrL2CacheSize, index));
    return 0;
}

static void print_device_json(const DeviceRecord& record,
                              const char *key = "device") {
    const auto& prop = record.properties;
    std::printf(
        "\"%s\":{\"cuda_index\":%d,\"pci_bdf\":\"%s\","
        "\"name\":\"%s\",\"compute_capability\":\"%d.%d\","
        "\"sm_count\":%d,\"vram_bytes\":%llu,\"memory_bus_width_bits\":%d,"
        "\"memory_clock_khz\":%d,\"l2_bytes\":%d}",
        key, record.index, json_escape(record.pci).c_str(),
        json_escape(prop.name).c_str(),
        prop.major, prop.minor, prop.multiProcessorCount,
        static_cast<unsigned long long>(prop.totalGlobalMem),
        record.memory_bus_width, record.memory_clock_khz, record.l2_bytes);
}

template <typename T>
__global__ void copy_kernel(T *dst, const T *src, size_t count) {
    size_t index = static_cast<size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
    size_t stride = static_cast<size_t>(gridDim.x) * blockDim.x;
    for (size_t i = index; i < count; i += stride)
        dst[i] = src[i];
}

/*
 * CUDA C++ has no scalar four-bit integer type.  These one-byte wrappers make
 * the storage operation explicit: each byte carries two independent logical
 * nibbles, low nibble first and high nibble second.  Signed INT4 interprets
 * each nibble as two's-complement [-8, 7]; UINT4 interprets it as [0, 15].
 * The copy kernel does no arithmetic, so both wrappers intentionally exercise
 * the same physical byte traffic while retaining distinct logical metadata.
 */
struct PackedInt4x2 { std::uint8_t storage; };
struct PackedUint4x2 { std::uint8_t storage; };

struct BandwidthResult {
    const char *dtype;
    size_t logical_element_bits;
    size_t storage_unit_bytes;
    size_t logical_elements_per_storage_unit;
    size_t logical_elements;
    const char *signedness;
    const char *packing;
    size_t bytes;
    int iterations;
    float milliseconds;
    double gbps;
    bool verified;
    bool telemetry_nvml_available;
    std::string telemetry_status;
    std::vector<TelemetrySample> telemetry_samples;
};

static bool valid_timing_ms(double milliseconds) {
    return milliseconds > 0.0 && std::isfinite(milliseconds);
}

static int verify_device_buffer(const void *buffer, size_t bytes,
                                unsigned char expected, bool *verified) {
    const size_t chunk_capacity = std::min<size_t>(bytes, 1U << 20);
    std::vector<unsigned char> chunk(chunk_capacity);
    const auto *device_bytes = static_cast<const unsigned char *>(buffer);
    *verified = true;
    for (size_t offset = 0; offset < bytes; offset += chunk_capacity) {
        const size_t count = std::min(chunk_capacity, bytes - offset);
        CUDA_OK(cudaMemcpy(
            chunk.data(), device_bytes + offset, count, cudaMemcpyDeviceToHost));
        if (!std::all_of(
                chunk.begin(), chunk.begin() + count,
                [expected](unsigned char value) { return value == expected; })) {
            *verified = false;
            break;
        }
    }
    return 0;
}

template <typename T>
static int measure_dtype(const char *name, size_t logical_element_bits,
                         size_t logical_elements_per_storage_unit,
                         const char *signedness, const char *packing,
                         size_t bytes, int iterations,
                         NvmlSampler *sampler, BandwidthResult *out) {
    if (!logical_element_bits || !logical_elements_per_storage_unit ||
        logical_element_bits * logical_elements_per_storage_unit !=
            sizeof(T) * CHAR_BIT) {
        std::fprintf(stderr, "invalid logical/storage type geometry\n");
        return 3;
    }
    bytes -= bytes % sizeof(T);
    size_t count = bytes / sizeof(T);
    T *a = nullptr, *b = nullptr;
    CUDA_OK(cudaMalloc(reinterpret_cast<void **>(&a), bytes));
    CUDA_OK(cudaMalloc(reinterpret_cast<void **>(&b), bytes));
    CUDA_OK(cudaMemset(a, 0x3c, bytes));
    CUDA_OK(cudaMemset(b, 0, bytes));
    int blocks = 4096;
    int threads = 256;
    copy_kernel<<<blocks, threads>>>(b, a, count);
    CUDA_OK(cudaGetLastError());
    CUDA_OK(cudaDeviceSynchronize());
    // The warm-up populated b. Poison it again before timing so a no-op timed
    // kernel cannot pass verification using warm-up or alternating-buffer residue.
    CUDA_OK(cudaMemset(b, 0xc3, bytes));
    CUDA_OK(cudaDeviceSynchronize());
    cudaEvent_t begin{}, end{};
    CUDA_OK(cudaEventCreate(&begin));
    CUDA_OK(cudaEventCreate(&end));
    sampler->start();
    CUDA_OK(cudaEventRecord(begin));
    for (int pass = 0; pass < iterations; ++pass) {
        copy_kernel<<<blocks, threads>>>(b, a, count);
    }
    CUDA_OK(cudaGetLastError());
    CUDA_OK(cudaEventRecord(end));
    CUDA_OK(cudaEventSynchronize(end));
    sampler->stop();
    float ms = 0;
    CUDA_OK(cudaEventElapsedTime(&ms, begin, end));
    if (!valid_timing_ms(ms)) {
        cudaEventDestroy(begin);
        cudaEventDestroy(end);
        cudaFree(a);
        cudaFree(b);
        std::fprintf(stderr, "invalid non-positive/non-finite CUDA timing\n");
        return 3;
    }
    bool full_buffer_verified = false;
    const int verify_rc = verify_device_buffer(
        b, bytes, 0x3c, &full_buffer_verified);
    cudaEventDestroy(begin);
    cudaEventDestroy(end);
    cudaFree(a);
    cudaFree(b);
    if (verify_rc != 0) return verify_rc;
    if (!full_buffer_verified) {
        std::fprintf(stderr, "full-buffer bandwidth verification failed\n");
        return 4;
    }
    out->dtype = name;
    out->logical_element_bits = logical_element_bits;
    out->storage_unit_bytes = sizeof(T);
    out->logical_elements_per_storage_unit =
        logical_elements_per_storage_unit;
    out->logical_elements =
        count * logical_elements_per_storage_unit;
    out->signedness = signedness;
    out->packing = packing;
    out->bytes = bytes;
    out->iterations = iterations;
    out->milliseconds = ms;
    out->gbps = (2.0 * static_cast<double>(bytes) * iterations) / (ms * 1.0e6);
    out->verified = full_buffer_verified;
    out->telemetry_nvml_available = sampler->ready();
    out->telemetry_status = sampler->status();
    out->telemetry_samples = sampler->samples();
    return 0;
}

static int bandwidth_command(int device, size_t bytes, int iterations) {
    CUDA_OK(cudaSetDevice(device));
    DeviceRecord device_record{};
    int device_rc = load_device_record(device, &device_record);
    if (device_rc != 0) return device_rc;
    NvmlSampler sampler(device);
    std::vector<BandwidthResult> results(14);
    int rc = measure_dtype<__half>(
        "float16", 16, 1, "floating", "native-scalar",
        bytes, iterations, &sampler, &results[0]);
    if (!rc) rc = measure_dtype<__nv_bfloat16>(
        "bfloat16", 16, 1, "floating", "native-scalar",
        bytes, iterations, &sampler, &results[1]);
    if (!rc) rc = measure_dtype<float>(
        "float32", 32, 1, "floating", "native-scalar",
        bytes, iterations, &sampler, &results[2]);
    if (!rc) rc = measure_dtype<double>(
        "float64", 64, 1, "floating", "native-scalar",
        bytes, iterations, &sampler, &results[3]);
    if (!rc) rc = measure_dtype<PackedInt4x2>(
        "int4", 4, 2, "signed", "two-nibbles-per-byte-low-then-high",
        bytes, iterations, &sampler, &results[4]);
    if (!rc) rc = measure_dtype<PackedUint4x2>(
        "uint4", 4, 2, "unsigned", "two-nibbles-per-byte-low-then-high",
        bytes, iterations, &sampler, &results[5]);
    if (!rc) rc = measure_dtype<std::int8_t>(
        "int8", 8, 1, "signed", "native-scalar",
        bytes, iterations, &sampler, &results[6]);
    if (!rc) rc = measure_dtype<std::uint8_t>(
        "uint8", 8, 1, "unsigned", "native-scalar",
        bytes, iterations, &sampler, &results[7]);
    if (!rc) rc = measure_dtype<std::int16_t>(
        "int16", 16, 1, "signed", "native-scalar",
        bytes, iterations, &sampler, &results[8]);
    if (!rc) rc = measure_dtype<std::uint16_t>(
        "uint16", 16, 1, "unsigned", "native-scalar",
        bytes, iterations, &sampler, &results[9]);
    if (!rc) rc = measure_dtype<std::int32_t>(
        "int32", 32, 1, "signed", "native-scalar",
        bytes, iterations, &sampler, &results[10]);
    if (!rc) rc = measure_dtype<std::uint32_t>(
        "uint32", 32, 1, "unsigned", "native-scalar",
        bytes, iterations, &sampler, &results[11]);
    if (!rc) rc = measure_dtype<std::int64_t>(
        "int64", 64, 1, "signed", "native-scalar",
        bytes, iterations, &sampler, &results[12]);
    if (!rc) rc = measure_dtype<std::uint64_t>(
        "uint64", 64, 1, "unsigned", "native-scalar",
        bytes, iterations, &sampler, &results[13]);
    if (rc) return rc;
    std::printf("{");
    std::printf("\"schema\":\"170hx-xray-cuda-bandwidth-v3\",");
    std::printf("\"operation\":\"global-memory-copy-kernel\",");
    std::printf("\"measurement_scope\":\"typed-load-store-memory-traffic-no-arithmetic\",");
    print_device_json(device_record);
    std::printf(",\"results\":[");
    for (size_t i = 0; i < results.size(); ++i) {
        const auto& r = results[i];
        if (i) std::printf(",");
        std::printf(
            "{\"dtype\":\"%s\",\"logical_element_bits\":%zu,"
            "\"storage_unit_bytes\":%zu,"
            "\"logical_elements_per_storage_unit\":%zu,"
            "\"logical_elements\":%zu,\"signedness\":\"%s\","
            "\"packing\":\"%s\",\"bytes\":%zu,"
            "\"iterations\":%d,\"milliseconds\":%.6f,"
            "\"traffic_definition\":\"one read plus one write\","
            "\"gbps\":%.6f,\"rate_unit\":\"decimal-GB/s\","
            "\"rate_scope\":\"physical-storage-bytes\",\"verified\":%s,"
            "\"verification_scope\":\"full-buffer-host-scan\","
            "\"telemetry\":",
            r.dtype, r.logical_element_bits, r.storage_unit_bytes,
            r.logical_elements_per_storage_unit, r.logical_elements,
            r.signedness, r.packing, r.bytes, r.iterations, r.milliseconds,
            r.gbps, r.verified ? "true" : "false");
        print_telemetry_object(
            r.telemetry_nvml_available, r.telemetry_status,
            r.telemetry_samples, "per-dtype-bracketing-window");
        std::printf("}");
    }
    std::vector<TelemetrySample> aggregate_samples;
    double aggregate_offset_seconds = 0.0;
    for (const auto& result : results) {
        for (const auto& original : result.telemetry_samples) {
            TelemetrySample sample = original;
            sample.seconds += aggregate_offset_seconds;
            aggregate_samples.push_back(sample);
        }
        if (!result.telemetry_samples.empty())
            aggregate_offset_seconds += result.telemetry_samples.back().seconds;
    }
    std::printf("],\"telemetry\":");
    print_telemetry_object(
        sampler.ready(), sampler.status(), aggregate_samples,
        "aggregate-of-per-dtype-bracketing-windows");
    std::printf(",\"integer_note\":\"INT4 and UINT4 are packed nibble storage; signedness labels interpretation only because this workload performs no arithmetic\"");
    std::printf(",\"float8_note\":\"GA100 has no native FP8 arithmetic; byte-sized memory traffic is not mislabeled as FP8\"}\n");
    return 0;
}

static int inventory_command() {
    int count = 0;
    CUDA_OK(cudaGetDeviceCount(&count));
    std::vector<DeviceRecord> records(count);
    for (int index = 0; index < count; ++index) {
        int rc = load_device_record(index, &records[index]);
        if (rc != 0) return rc;
    }
    std::printf("{\"schema\":\"170hx-xray-cuda-inventory-v1\",\"devices\":[");
    for (int index = 0; index < count; ++index) {
        if (index) std::printf(",");
        std::printf("{");
        print_device_json(records[index], "record");
        std::printf("}");
    }
    std::printf("]}\n");
    return 0;
}

struct CopyMeasurement {
    size_t bytes;
    int iterations;
    double direct_ms;
    double direct_gbps;
    double host_ms;
    double host_gbps;
    bool direct_available;
    bool direct_verified;
    bool host_verified;
};

struct DirectionMeasurement {
    int source;
    int destination;
    bool destination_can_access_source;
    bool peer_access_enabled;
    bool all_copies_verified;
    std::vector<CopyMeasurement> results;
};

static double event_elapsed(cudaEvent_t begin, cudaEvent_t end) {
    float ms = 0;
    if (cudaEventElapsedTime(&ms, begin, end) != cudaSuccess)
        return -1.0;
    return ms;
}

static int measure_copy_direction(
        int source, int destination, bool destination_can_access_source,
        const std::vector<size_t>& sizes, int iterations,
        unsigned char pattern, DirectionMeasurement *direction) {
    size_t maximum = *std::max_element(sizes.begin(), sizes.end());
    void *src = nullptr, *dst = nullptr, *host = nullptr;
    direction->source = source;
    direction->destination = destination;
    direction->destination_can_access_source = destination_can_access_source;
    direction->peer_access_enabled = false;
    direction->all_copies_verified = true;
    direction->results.clear();
    CUDA_OK(cudaSetDevice(source));
    CUDA_OK(cudaMalloc(&src, maximum));
    CUDA_OK(cudaMemset(src, pattern, maximum));
    CUDA_OK(cudaDeviceSynchronize());
    CUDA_OK(cudaSetDevice(destination));
    CUDA_OK(cudaMalloc(&dst, maximum));
    if (destination_can_access_source) {
        cudaError_t e = cudaDeviceEnablePeerAccess(source, 0);
        if (e == cudaErrorPeerAccessAlreadyEnabled) {
            cudaGetLastError();
            direction->peer_access_enabled = true;
        } else if (e == cudaSuccess) {
            direction->peer_access_enabled = true;
        } else {
            std::fprintf(stderr,
                         "unable to enable destination-to-source peer access: %s\n",
                         cudaGetErrorString(e));
            CUDA_OK(cudaSetDevice(source)); cudaFree(src);
            CUDA_OK(cudaSetDevice(destination)); cudaFree(dst);
            return 2;
        }
    }
    CUDA_OK(cudaHostAlloc(&host, maximum, cudaHostAllocPortable));
    bool all_timings_valid = true;
    for (size_t bytes : sizes) {
        CopyMeasurement result{};
        result.bytes = bytes;
        result.iterations = iterations;
        result.direct_available = direction->peer_access_enabled;
        if (result.direct_available) {
            CUDA_OK(cudaSetDevice(destination));
            CUDA_OK(cudaMemset(dst, pattern ^ 0xffU, bytes));
            CUDA_OK(cudaDeviceSynchronize());
            cudaStream_t stream{};
            cudaEvent_t begin{}, end{};
            CUDA_OK(cudaStreamCreate(&stream));
            CUDA_OK(cudaEventCreate(&begin));
            CUDA_OK(cudaEventCreate(&end));
            CUDA_OK(cudaMemcpyPeerAsync(dst, destination, src, source, bytes, stream));
            CUDA_OK(cudaStreamSynchronize(stream));
            CUDA_OK(cudaMemsetAsync(dst, pattern ^ 0xffU, bytes, stream));
            CUDA_OK(cudaStreamSynchronize(stream));
            CUDA_OK(cudaEventRecord(begin, stream));
            for (int i = 0; i < iterations; ++i)
                CUDA_OK(cudaMemcpyPeerAsync(dst, destination, src, source, bytes, stream));
            CUDA_OK(cudaEventRecord(end, stream));
            CUDA_OK(cudaEventSynchronize(end));
            const double direct_total_ms = event_elapsed(begin, end);
            if (!valid_timing_ms(direct_total_ms)) {
                all_timings_valid = false;
            } else {
                result.direct_ms = direct_total_ms / iterations;
                result.direct_gbps = static_cast<double>(bytes) /
                                     (result.direct_ms * 1.0e6);
            }
            cudaEventDestroy(begin); cudaEventDestroy(end); cudaStreamDestroy(stream);
            if (!all_timings_valid) break;
            int verify_rc = verify_device_buffer(
                dst, bytes, pattern, &result.direct_verified);
            if (verify_rc != 0) {
                CUDA_OK(cudaSetDevice(source)); cudaFree(src);
                CUDA_OK(cudaSetDevice(destination)); cudaFree(dst);
                cudaFreeHost(host);
                return verify_rc;
            }
            direction->all_copies_verified = (
                direction->all_copies_verified && result.direct_verified);
        }
        // Warm both staged legs, then poison the destination again so the
        // measured transfer cannot pass verification using direct-copy residue.
        CUDA_OK(cudaSetDevice(source));
        CUDA_OK(cudaMemcpy(host, src, bytes, cudaMemcpyDeviceToHost));
        CUDA_OK(cudaSetDevice(destination));
        CUDA_OK(cudaMemcpy(dst, host, bytes, cudaMemcpyHostToDevice));
        std::memset(host, pattern ^ 0xffU, bytes);
        CUDA_OK(cudaMemset(dst, pattern ^ 0xffU, bytes));
        CUDA_OK(cudaDeviceSynchronize());
        auto host_begin = std::chrono::steady_clock::now();
        for (int i = 0; i < iterations; ++i) {
            CUDA_OK(cudaSetDevice(source));
            CUDA_OK(cudaMemcpy(host, src, bytes, cudaMemcpyDeviceToHost));
            CUDA_OK(cudaSetDevice(destination));
            CUDA_OK(cudaMemcpy(dst, host, bytes, cudaMemcpyHostToDevice));
        }
        auto host_end = std::chrono::steady_clock::now();
        result.host_ms = std::chrono::duration<double, std::milli>(
            host_end - host_begin).count() / iterations;
        if (!valid_timing_ms(result.host_ms)) {
            all_timings_valid = false;
            break;
        }
        result.host_gbps = static_cast<double>(bytes) / (result.host_ms * 1.0e6);
        CUDA_OK(cudaSetDevice(destination));
        int verify_rc = verify_device_buffer(
            dst, bytes, pattern, &result.host_verified);
        if (verify_rc != 0) {
            CUDA_OK(cudaSetDevice(source)); cudaFree(src);
            CUDA_OK(cudaSetDevice(destination)); cudaFree(dst);
            cudaFreeHost(host);
            return verify_rc;
        }
        direction->all_copies_verified = (
            direction->all_copies_verified && result.host_verified);
        direction->results.push_back(result);
    }
    CUDA_OK(cudaSetDevice(source)); cudaFree(src);
    CUDA_OK(cudaSetDevice(destination)); cudaFree(dst);
    cudaFreeHost(host);
    if (!all_timings_valid) {
        std::fprintf(stderr, "invalid non-positive/non-finite copy timing\n");
        return 3;
    }
    return 0;
}

static void print_direction_json(const DirectionMeasurement& direction,
                                 const DeviceRecord& source,
                                 const DeviceRecord& destination) {
    std::printf(
        "{\"from\":{\"cuda_index\":%d,\"pci_bdf\":\"%s\"},"
        "\"to\":{\"cuda_index\":%d,\"pci_bdf\":\"%s\"},"
        "\"destination_can_access_source\":%s,"
        "\"peer_access_enabled\":%s,\"results\":[",
        source.index, json_escape(source.pci).c_str(),
        destination.index, json_escape(destination.pci).c_str(),
        direction.destination_can_access_source ? "true" : "false",
        direction.peer_access_enabled ? "true" : "false");
    for (size_t i = 0; i < direction.results.size(); ++i) {
        const auto& r = direction.results[i];
        if (i) std::printf(",");
        std::printf("{\"bytes\":%zu,\"iterations\":%d,", r.bytes, r.iterations);
        if (r.direct_available) {
            std::printf(
                "\"direct\":{\"available\":true,\"latency_ms\":%.9f,"
                "\"gbps\":%.6f,\"rate_unit\":\"decimal-GB/s\","
                "\"full_buffer_verified\":%s},",
                r.direct_ms, r.direct_gbps,
                r.direct_verified ? "true" : "false");
        } else {
            std::printf(
                "\"direct\":{\"available\":false,\"latency_ms\":null,"
                "\"gbps\":null,\"rate_unit\":\"decimal-GB/s\","
                "\"full_buffer_verified\":null},");
        }
        std::printf(
            "\"host_staged\":{\"path\":\"D2H-then-H2D-pinned\","
            "\"latency_ms\":%.9f,\"gbps\":%.6f,"
            "\"rate_unit\":\"decimal-GB/s\","
            "\"full_buffer_verified\":%s}}",
            r.host_ms, r.host_gbps, r.host_verified ? "true" : "false");
    }
    std::printf(
        "],\"all_copies_verified\":%s}",
        direction.all_copies_verified ? "true" : "false");
}

static int p2p_command(int source, int destination,
                       const std::vector<size_t>& sizes, int iterations) {
    DeviceRecord source_record{}, destination_record{};
    int device_rc = load_device_record(source, &source_record);
    if (device_rc != 0) return device_rc;
    device_rc = load_device_record(destination, &destination_record);
    if (device_rc != 0) return device_rc;
    int source_can_access_destination = 0;
    int destination_can_access_source = 0;
    CUDA_OK(cudaDeviceCanAccessPeer(
        &source_can_access_destination, source, destination));
    CUDA_OK(cudaDeviceCanAccessPeer(
        &destination_can_access_source, destination, source));
    DirectionMeasurement forward{}, reverse{};
    int rc = measure_copy_direction(
        source, destination, destination_can_access_source != 0,
        sizes, iterations, 0x5a, &forward);
    if (rc != 0) return rc;
    rc = measure_copy_direction(
        destination, source, source_can_access_destination != 0,
        sizes, iterations, 0xa5, &reverse);
    if (rc != 0) return rc;

    std::printf("{\"schema\":\"170hx-xray-p2p-v2\",");
    print_device_json(source_record, "source");
    std::printf(",");
    print_device_json(destination_record, "destination");
    std::printf(",\"peer_access_capability\":{"
                "\"source_can_access_destination\":%s,"
                "\"destination_can_access_source\":%s},\"directions\":[",
                source_can_access_destination ? "true" : "false",
                destination_can_access_source ? "true" : "false");
    print_direction_json(forward, source_record, destination_record);
    std::printf(",");
    print_direction_json(reverse, destination_record, source_record);
    const bool all_copies_verified = (
        forward.all_copies_verified && reverse.all_copies_verified);
    std::printf(
        "],\"verification_scope\":\"full-buffer-host-scan-outside-timed-region\","
        "\"all_copies_verified\":%s,"
        "\"payload_bandwidth_note\":\"host-staged GB/s counts payload once although it traverses PCIe twice\"}\n",
        all_copies_verified ? "true" : "false");
    if (!all_copies_verified) {
        std::fprintf(stderr, "one or more P2P copy verifications failed\n");
        return 4;
    }
    return 0;
}

static const size_t MAX_BUFFER_BYTES = 4ULL << 30;
static const int MAX_ITERATIONS = 10000;

static bool parse_size(const char *text, size_t *result) {
    if (!text || !*text || *text < '0' || *text > '9') return false;
    errno = 0;
    char *end = nullptr;
    unsigned long long value = std::strtoull(text, &end, 10);
    if (errno == ERANGE || !end || end == text) return false;
    unsigned long long scale = 1;
    if ((*end == 'K' || *end == 'k') && end[1] == '\0') scale = 1024ULL;
    else if ((*end == 'M' || *end == 'm') && end[1] == '\0')
        scale = 1024ULL * 1024ULL;
    else if ((*end == 'G' || *end == 'g') && end[1] == '\0')
        scale = 1024ULL * 1024ULL * 1024ULL;
    else if (*end != '\0') return false;
    if (value > static_cast<unsigned long long>(SIZE_MAX) / scale) return false;
    value *= scale;
    if (value == 0 || value > MAX_BUFFER_BYTES) return false;
    *result = static_cast<size_t>(value);
    return true;
}

static bool parse_int(const char *text, int *result) {
    if (!text || !*text || *text < '0' || *text > '9') return false;
    errno = 0;
    char *end = nullptr;
    long value = std::strtol(text, &end, 10);
    if (errno == ERANGE || !end || *end != '\0' || value > INT_MAX) return false;
    *result = static_cast<int>(value);
    return true;
}

static void usage(const char *name) {
    std::fprintf(stderr,
        "usage:\n"
        "  %s inventory\n"
        "  %s bandwidth --device N [--bytes 512M] [--iterations 50]\n"
        "  %s p2p --source N --destination N [--min 4K] [--max 1G] [--iterations 20]\n",
        name, name, name);
}

int main(int argc, char **argv) {
    if (argc < 2) { usage(argv[0]); return 2; }
    std::string command = argv[1];
    int device = -1, source = -1, destination = -1, iterations = 20;
    size_t bytes = 512ULL << 20, minimum = 4ULL << 10, maximum = 1ULL << 30;
    bool device_seen = false, source_seen = false, destination_seen = false;
    bool iterations_seen = false, bytes_seen = false;
    bool minimum_seen = false, maximum_seen = false;
    for (int i = 2; i < argc; ++i) {
        if (!std::strcmp(argv[i], "--device") && i + 1 < argc) {
            if (device_seen) { usage(argv[0]); return 2; }
            device_seen = true;
            if (!parse_int(argv[++i], &device)) { usage(argv[0]); return 2; }
        } else if (!std::strcmp(argv[i], "--source") && i + 1 < argc) {
            if (source_seen) { usage(argv[0]); return 2; }
            source_seen = true;
            if (!parse_int(argv[++i], &source)) { usage(argv[0]); return 2; }
        } else if (!std::strcmp(argv[i], "--destination") && i + 1 < argc) {
            if (destination_seen) { usage(argv[0]); return 2; }
            destination_seen = true;
            if (!parse_int(argv[++i], &destination)) { usage(argv[0]); return 2; }
        } else if (!std::strcmp(argv[i], "--iterations") && i + 1 < argc) {
            if (iterations_seen) { usage(argv[0]); return 2; }
            iterations_seen = true;
            if (!parse_int(argv[++i], &iterations)) { usage(argv[0]); return 2; }
        } else if (!std::strcmp(argv[i], "--bytes") && i + 1 < argc) {
            if (bytes_seen) { usage(argv[0]); return 2; }
            bytes_seen = true;
            if (!parse_size(argv[++i], &bytes)) { usage(argv[0]); return 2; }
        } else if (!std::strcmp(argv[i], "--min") && i + 1 < argc) {
            if (minimum_seen) { usage(argv[0]); return 2; }
            minimum_seen = true;
            if (!parse_size(argv[++i], &minimum)) { usage(argv[0]); return 2; }
        } else if (!std::strcmp(argv[i], "--max") && i + 1 < argc) {
            if (maximum_seen) { usage(argv[0]); return 2; }
            maximum_seen = true;
            if (!parse_size(argv[++i], &maximum)) { usage(argv[0]); return 2; }
        }
        else { usage(argv[0]); return 2; }
    }
    if (iterations <= 0 || iterations > MAX_ITERATIONS) {
        std::fprintf(stderr, "iterations must be in [1, %d]\n", MAX_ITERATIONS);
        return 2;
    }
    if (command == "inventory") {
        if (device_seen || source_seen || destination_seen || iterations_seen ||
            bytes_seen || minimum_seen || maximum_seen) {
            usage(argv[0]); return 2;
        }
        return inventory_command();
    }
    if (command == "bandwidth") {
        if (device < 0 || bytes < 4096 || source_seen || destination_seen ||
            minimum_seen || maximum_seen) { usage(argv[0]); return 2; }
        return bandwidth_command(device, bytes, iterations);
    }
    if (command == "p2p") {
        if (source < 0 || destination < 0 || source == destination ||
            device_seen || bytes_seen ||
            minimum < 4 || maximum < minimum || (minimum & (minimum - 1)) ||
            (maximum & (maximum - 1))) {
            usage(argv[0]); return 2;
        }
        std::vector<size_t> sizes;
        for (size_t value = minimum; value <= maximum; value <<= 1) {
            sizes.push_back(value);
            if (value > maximum / 2) break;
        }
        return p2p_command(source, destination, sizes, iterations);
    }
    usage(argv[0]);
    return 2;
}
