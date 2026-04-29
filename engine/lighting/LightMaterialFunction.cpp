// M5 - LightMaterialFunction implementation.
//
// JSON serialization uses nova::core::Json (engine/core/json_wrapper.hpp);
// nlohmann/json is intentionally not pulled in here — the wrapper insulates
// the engine from third-party JSON dependencies and keeps build deps minimal.

#include "LightMaterialFunction.hpp"

#include <stdexcept>

namespace automatica {
namespace lighting {

namespace {

// Read a numeric Json value as float, accepting both Int and Double encodings.
float ReadFloat(const nova::core::Json& j) {
    if (j.is_number()) return static_cast<float>(j.as_double());
    throw std::runtime_error("LightMaterialFunction: expected numeric value");
}

}  // namespace

// -----------------------------------------------------------------------------
// Serialize
//
// Layout:
//   {
//     "functionType": "<string>",
//     "parameters":   { "<name>": <float>, ... },
//     "curveData":    [ {"x": <float>, "y": <float>}, ... ]
//   }
nova::core::Json LightMaterialFunction::Serialize() const {
    nova::core::Json out = nova::core::Json::Object();

    out["functionType"] = nova::core::Json(function_type_);

    nova::core::Json params = nova::core::Json::Object();
    for (const auto& kv : parameters_) {
        params[kv.first] = nova::core::Json(static_cast<double>(kv.second));
    }
    out["parameters"] = params;

    nova::core::Json curve = nova::core::Json::Array();
    for (const auto& p : curve_data_) {
        nova::core::Json pt = nova::core::Json::Object();
        pt["x"] = nova::core::Json(static_cast<double>(p.x));
        pt["y"] = nova::core::Json(static_cast<double>(p.y));
        curve.push_back(pt);
    }
    out["curveData"] = curve;

    return out;
}

// -----------------------------------------------------------------------------
// Deserialize
//
// Reads functionType (required, string), parameters (optional object of
// floats), and curveData (optional array of {x,y}). Any pre-existing state on
// *this is cleared before population so deserialization is idempotent.
void LightMaterialFunction::Deserialize(const nova::core::Json& j) {
    if (!j.is_object()) {
        throw std::runtime_error(
            "LightMaterialFunction::Deserialize: expected JSON object");
    }

    if (!j.contains("functionType") || !j.at("functionType").is_string()) {
        throw std::runtime_error(
            "LightMaterialFunction::Deserialize: missing/invalid functionType");
    }
    function_type_ = j.at("functionType").as_string();

    parameters_.clear();
    if (j.contains("parameters")) {
        const auto& params = j.at("parameters");
        if (!params.is_object()) {
            throw std::runtime_error(
                "LightMaterialFunction::Deserialize: parameters must be an object");
        }
        for (const auto& kv : params.items()) {
            parameters_[kv.first] = ReadFloat(kv.second);
        }
    }

    curve_data_.clear();
    if (j.contains("curveData")) {
        const auto& curve = j.at("curveData");
        if (!curve.is_array()) {
            throw std::runtime_error(
                "LightMaterialFunction::Deserialize: curveData must be an array");
        }
        curve_data_.reserve(curve.size());
        for (const auto& pt : curve.array_items()) {
            if (!pt.is_object() || !pt.contains("x") || !pt.contains("y")) {
                throw std::runtime_error(
                    "LightMaterialFunction::Deserialize: curveData entry must be {x,y}");
            }
            curve_data_.emplace_back(ReadFloat(pt.at("x")), ReadFloat(pt.at("y")));
        }
    }
}

}  // namespace lighting
}  // namespace automatica
