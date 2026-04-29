// M5 - LightMaterialFunction
//
// Models a parameterized light/material response function used by the lighting
// pipeline to compose attenuation, falloff, color, and ramp curves. Each
// function instance has:
//   * a string `functionType`     — selects the analytic form
//                                   ("constant", "linear", "exponential",
//                                    "smoothstep", "curve")
//   * a `parameters` map<string,float> — named scalar coefficients
//   * a `curveData` vec2 array         — control points (x,y) for the
//                                        "curve" function type, or auxiliary
//                                        sampling points for other types
//
// The class supports JSON round-trip via the project's nova::core::Json
// wrapper (see engine/core/json_wrapper.hpp). nlohmann/json is intentionally
// not used directly — the wrapper isolates the engine from third-party
// dependencies.

#pragma once

#include <map>
#include <string>
#include <vector>

#include "../core/json_wrapper.hpp"

namespace automatica {
namespace lighting {

// 2D control point. Kept as a plain struct so it serializes cleanly without
// pulling in the project's full math types.
struct Vec2 {
    float x = 0.0f;
    float y = 0.0f;

    Vec2() = default;
    Vec2(float x_, float y_) : x(x_), y(y_) {}

    bool operator==(const Vec2& o) const { return x == o.x && y == o.y; }
    bool operator!=(const Vec2& o) const { return !(*this == o); }
};

class LightMaterialFunction {
public:
    LightMaterialFunction() = default;
    explicit LightMaterialFunction(std::string function_type)
        : function_type_(std::move(function_type)) {}

    // --- Function type ----------------------------------------------------
    const std::string& FunctionType() const { return function_type_; }
    void SetFunctionType(const std::string& t) { function_type_ = t; }

    // --- Parameters (name -> scalar) --------------------------------------
    void SetParameter(const std::string& name, float value) {
        parameters_[name] = value;
    }
    bool HasParameter(const std::string& name) const {
        return parameters_.find(name) != parameters_.end();
    }
    float GetParameter(const std::string& name, float fallback = 0.0f) const {
        auto it = parameters_.find(name);
        return it == parameters_.end() ? fallback : it->second;
    }
    const std::map<std::string, float>& Parameters() const { return parameters_; }

    // --- Curve control points --------------------------------------------
    void AddCurvePoint(const Vec2& p) { curve_data_.push_back(p); }
    void AddCurvePoint(float x, float y) { curve_data_.emplace_back(x, y); }
    void ClearCurve() { curve_data_.clear(); }
    const std::vector<Vec2>& CurveData() const { return curve_data_; }

    // --- JSON serialization ----------------------------------------------
    // Serialize: writes functionType (string), parameters (object of floats),
    // and curveData (array of {x,y} objects) into a Json object.
    nova::core::Json Serialize() const;

    // Deserialize: replaces the contents of *this from `j`. Missing optional
    // fields are tolerated; a missing or non-string functionType throws.
    void Deserialize(const nova::core::Json& j);

    // Equality is structural on all three persisted fields. Used by the
    // round-trip test to verify lossless serialization.
    bool operator==(const LightMaterialFunction& o) const {
        return function_type_ == o.function_type_
            && parameters_     == o.parameters_
            && curve_data_     == o.curve_data_;
    }
    bool operator!=(const LightMaterialFunction& o) const { return !(*this == o); }

private:
    std::string                  function_type_;
    std::map<std::string, float> parameters_;
    std::vector<Vec2>            curve_data_;
};

}  // namespace lighting
}  // namespace automatica
