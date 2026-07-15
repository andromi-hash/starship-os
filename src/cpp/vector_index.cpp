#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>
#include <cmath>
#include <vector>
#include <algorithm>
#include <numeric>
#include <cstring>

namespace py = pybind11;

class EmbeddingProcessor {
public:
    static py::array_t<float> normalize(py::array_t<float> vectors, bool inplace = false) {
        auto buf = vectors.request();
        float* data = static_cast<float*>(buf.ptr);
        size_t rows = buf.shape[0];
        size_t dim = buf.shape[1];

        py::array_t<float> result;
        float* out;
        if (inplace) {
            result = vectors;
            out = data;
        } else {
            result = py::array_t<float>(buf.size);
            result.resize({rows, dim});
            out = static_cast<float*>(result.request().ptr);
            std::memcpy(out, data, buf.size * sizeof(float));
        }

        #pragma omp parallel for if(rows > 100)
        for (size_t i = 0; i < rows; i++) {
            float* vec = out + i * dim;
            float sum = 0.0f;
            for (size_t j = 0; j < dim; j++) sum += vec[j] * vec[j];
            float inv = (sum > 1e-12f) ? 1.0f / std::sqrt(sum) : 0.0f;
            for (size_t j = 0; j < dim; j++) vec[j] *= inv;
        }

        return result;
    }

    static py::array_t<float> mean_pool(py::array_t<float> vectors, py::array_t<int64_t> segment_ids, int num_segments) {
        auto vbuf = vectors.request();
        auto sbuf = segment_ids.request();
        float* vdata = static_cast<float*>(vbuf.ptr);
        int64_t* segs = static_cast<int64_t*>(sbuf.ptr);
        size_t rows = vbuf.shape[0];
        size_t dim = vbuf.shape[1];

        py::array_t<float> result;
        result.resize({(size_t)num_segments, dim});
        float* out = static_cast<float*>(result.request().ptr);
        std::memset(out, 0, num_segments * dim * sizeof(float));

        std::vector<int> counts(num_segments, 0);
        for (size_t i = 0; i < rows; i++) {
            int sid = (int)segs[i];
            if (sid < 0 || sid >= num_segments) continue;
            float* vec = vdata + i * dim;
            float* target = out + sid * dim;
            for (size_t j = 0; j < dim; j++) target[j] += vec[j];
            counts[sid]++;
        }

        for (int i = 0; i < num_segments; i++) {
            if (counts[i] > 0) {
                float inv = 1.0f / counts[i];
                float* target = out + i * dim;
                for (size_t j = 0; j < dim; j++) target[j] *= inv;
            }
        }

        return result;
    }

    static py::array_t<float> batch_dot(py::array_t<float> a, py::array_t<float> b) {
        auto abuf = a.request(), bbuf = b.request();
        float* adata = static_cast<float*>(abuf.ptr);
        float* bdata = static_cast<float*>(bbuf.ptr);
        size_t rows = abuf.shape[0];
        size_t dim = abuf.shape[1];

        py::array_t<float> result(rows);
        float* out = static_cast<float*>(result.request().ptr);

        #pragma omp parallel for if(rows > 100)
        for (size_t i = 0; i < rows; i++) {
            float* va = adata + i * dim;
            float* vb = bdata + i * dim;
            float dot = 0.0f;
            for (size_t j = 0; j < dim; j++) dot += va[j] * vb[j];
            out[i] = dot;
        }

        return result;
    }
};

PYBIND11_MODULE(vector_index, m) {
    m.doc() = "Starship OS C++ Vector Processor — fast embedding normalization and pooling";

    py::class_<EmbeddingProcessor>(m, "EmbeddingProcessor")
        .def_static("normalize", &EmbeddingProcessor::normalize,
                    py::arg("vectors"), py::arg("inplace") = false,
                    "L2-normalize embeddings in batch. 10-50x faster than NumPy for large batches.")
        .def_static("mean_pool", &EmbeddingProcessor::mean_pool,
                    py::arg("vectors"), py::arg("segment_ids"), py::arg("num_segments"),
                    "Mean-pool embeddings by segment IDs. Used for pooling token embeddings.")
        .def_static("batch_dot", &EmbeddingProcessor::batch_dot,
                    py::arg("a"), py::arg("b"),
                    "Compute batch dot products between two sets of vectors.");
}
