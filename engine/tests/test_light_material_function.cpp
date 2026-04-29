// M5 - Round-trip test for LightMaterialFunction JSON serialization.
//
// Verifies that Serialize -> dump -> parse -> Deserialize produces a value
// equal to the original (function type, parameter map, and curve points).

#include <cassert>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <stdexcept>

#include "../lighting/LightMaterialFunction.hpp"

using automatica::lighting::LightMaterialFunction;
using automatica::lighting::Vec2;
using nova::core::Json;

// ---- Minimal test framework (matches nova_unit_tests style) ----

static int g_passed = 0;
static int g_failed = 0;
static int g_total  = 0;

#define TEST(name) \
    static void test_##name(); \
    struct TestReg_##name { \
        TestReg_##name() { \
            g_total++; \
            std::cout << "  [RUN ] " << #name << std::endl; \
            try { \
                test_##name(); \
                g_passed++; \
                std::cout << "  [PASS] " << #name << std::endl; \
            } catch (const std::exception& e) { \
                g_failed++; \
                std::cout << "  [FAIL] " << #name << ": " << e.what() << std::endl; \
            } \
        } \
    }; \
    static TestReg_##name g_reg_##name; \
    static void test_##name()

#define ASSERT_TRUE(cond) \
    if (!(cond)) { \
        std::ostringstream ss; \
        ss << "ASSERT_TRUE failed at " << __FILE__ << ":" << __LINE__; \
        throw std::runtime_error(ss.str()); \
    }

#define ASSERT_EQ(a, b) \
    if (!((a) == (b))) { \
        std::ostringstream ss; \
        ss << "ASSERT_EQ failed at " << __FILE__ << ":" << __LINE__; \
        throw std::runtime_error(ss.str()); \
    }

// ---- Tests ----

TEST(serialize_writes_function_type_and_fields) {
    LightMaterialFunction f("smoothstep");
    f.SetParameter("intensity", 2.5f);
    f.SetParameter("falloff",   0.75f);
    f.AddCurvePoint(0.0f, 0.0f);
    f.AddCurvePoint(0.5f, 0.8f);
    f.AddCurvePoint(1.0f, 1.0f);

    Json j = f.Serialize();
    ASSERT_TRUE(j.is_object());
    ASSERT_TRUE(j.contains("functionType"));
    ASSERT_EQ(j.at("functionType").as_string(), std::string("smoothstep"));

    ASSERT_TRUE(j.at("parameters").is_object());
    ASSERT_TRUE(j.at("parameters").contains("intensity"));
    ASSERT_TRUE(j.at("parameters").contains("falloff"));

    ASSERT_TRUE(j.at("curveData").is_array());
    ASSERT_EQ(j.at("curveData").size(), (size_t)3);
    ASSERT_TRUE(j.at("curveData")[1].contains("x"));
    ASSERT_TRUE(j.at("curveData")[1].contains("y"));
}

TEST(roundtrip_preserves_all_fields) {
    LightMaterialFunction src("exponential");
    src.SetParameter("k",      4.25f);
    src.SetParameter("offset", -1.5f);
    src.SetParameter("scale",  10.0f);
    src.AddCurvePoint(0.0f,  0.0f);
    src.AddCurvePoint(0.25f, 0.0625f);
    src.AddCurvePoint(0.5f,  0.25f);
    src.AddCurvePoint(1.0f,  1.0f);

    // Serialize -> dump -> parse -> Deserialize.
    Json j   = src.Serialize();
    std::string text = j.dump();
    Json parsed = Json::parse(text);

    LightMaterialFunction dst;
    dst.Deserialize(parsed);

    ASSERT_EQ(dst.FunctionType(), std::string("exponential"));
    ASSERT_EQ(dst.Parameters().size(), (size_t)3);
    ASSERT_EQ(dst.GetParameter("k"),      4.25f);
    ASSERT_EQ(dst.GetParameter("offset"), -1.5f);
    ASSERT_EQ(dst.GetParameter("scale"),  10.0f);

    ASSERT_EQ(dst.CurveData().size(), (size_t)4);
    ASSERT_TRUE(dst.CurveData()[0] == Vec2(0.0f, 0.0f));
    ASSERT_TRUE(dst.CurveData()[1] == Vec2(0.25f, 0.0625f));
    ASSERT_TRUE(dst.CurveData()[2] == Vec2(0.5f, 0.25f));
    ASSERT_TRUE(dst.CurveData()[3] == Vec2(1.0f, 1.0f));

    // Structural equality.
    ASSERT_TRUE(src == dst);
}

TEST(roundtrip_empty_parameters_and_curve) {
    LightMaterialFunction src("constant");
    Json j = src.Serialize();
    LightMaterialFunction dst;
    dst.Deserialize(Json::parse(j.dump()));

    ASSERT_EQ(dst.FunctionType(), std::string("constant"));
    ASSERT_TRUE(dst.Parameters().empty());
    ASSERT_TRUE(dst.CurveData().empty());
    ASSERT_TRUE(src == dst);
}

TEST(deserialize_rejects_missing_function_type) {
    Json j = Json::Object();
    j["parameters"] = Json::Object();
    j["curveData"]  = Json::Array();
    LightMaterialFunction dst;
    bool threw = false;
    try { dst.Deserialize(j); } catch (const std::exception&) { threw = true; }
    ASSERT_TRUE(threw);
}

TEST(deserialize_clears_existing_state) {
    LightMaterialFunction dst("old_type");
    dst.SetParameter("stale", 99.0f);
    dst.AddCurvePoint(7.0f, 7.0f);

    Json j = Json::Object();
    j["functionType"] = Json(std::string("new_type"));
    Json params = Json::Object();
    params["fresh"] = Json(1.5);
    j["parameters"] = params;
    j["curveData"] = Json::Array();

    dst.Deserialize(j);
    ASSERT_EQ(dst.FunctionType(), std::string("new_type"));
    ASSERT_EQ(dst.Parameters().size(), (size_t)1);
    ASSERT_TRUE(dst.HasParameter("fresh"));
    ASSERT_TRUE(!dst.HasParameter("stale"));
    ASSERT_TRUE(dst.CurveData().empty());
}

int main() {
    std::cout << "Running LightMaterialFunction tests (" << g_total << " registered)\n";
    std::cout << "----------------------------------------\n";
    std::cout << "Total:  " << g_total  << "\n";
    std::cout << "Passed: " << g_passed << "\n";
    std::cout << "Failed: " << g_failed << "\n";
    return g_failed == 0 ? 0 : 1;
}
