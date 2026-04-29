cmake_minimum_required(VERSION 3.10)
project(Nova3DEngine)

set(CMAKE_CXX_STANDARD 17)

# Find TBB for parallel execution policies
find_package(TBB QUIET)

add_library(TextureImporter import/TextureImporter.cpp)
add_library(ModelImporter import/ModelImporter.cpp)

add_library(VoxelTerrain terrain/VoxelTerrain.cpp)
target_include_directories(VoxelTerrain PUBLIC terrain)

add_library(SDFTerrain terrain/SDFTerrain.cpp)
target_include_directories(SDFTerrain PUBLIC terrain)
target_link_libraries(SDFTerrain PUBLIC VoxelTerrain)
if(TBB_FOUND)
    target_link_libraries(SDFTerrain PUBLIC TBB::tbb)
endif()

# Authoritative game server (MMO-SERVER-1)
add_library(GameServer server/GameServer.cpp)
target_include_directories(GameServer PUBLIC server)
find_package(Threads REQUIRED)
target_link_libraries(GameServer PUBLIC Threads::Threads)

# Player authentication / JWT (MMO-SERVER-3)
add_library(AuthService server/AuthService.cpp)
target_include_directories(AuthService PUBLIC server)
find_package(SQLite3 QUIET)
if(SQLite3_FOUND)
    target_link_libraries(AuthService PUBLIC SQLite::SQLite3)
endif()
find_library(BCRYPT_LIB bcrypt)
if(BCRYPT_LIB)
    target_link_libraries(AuthService PUBLIC ${BCRYPT_LIB})
endif()

# REST API for player and world data (MMO-SERVER-4)
add_library(RestApi server/RestApi.cpp)
target_include_directories(RestApi PUBLIC server core)
target_link_libraries(RestApi PUBLIC AuthService)

# Persistent world state (PERSIST-1)
# Prefer system SQLite3; fall back to the vendored amalgamation under
# persistence/sqlite_amalgamation/ so the build is self-contained.
if(SQLite3_FOUND)
    add_library(WorldStateRepository persistence/WorldStateRepository.cpp)
    target_include_directories(WorldStateRepository PUBLIC persistence)
    target_link_libraries(WorldStateRepository PUBLIC SQLite::SQLite3)
elseif(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/persistence/sqlite_amalgamation/sqlite3.c")
    add_library(SQLiteAmalgamation STATIC
        persistence/sqlite_amalgamation/sqlite3.c)
    target_include_directories(SQLiteAmalgamation PUBLIC
        persistence/sqlite_amalgamation)
    target_compile_definitions(SQLiteAmalgamation PRIVATE
        SQLITE_THREADSAFE=1
        SQLITE_OMIT_LOAD_EXTENSION=1)
    if(MSVC)
        target_compile_options(SQLiteAmalgamation PRIVATE /w)
    else()
        target_compile_options(SQLiteAmalgamation PRIVATE -w)
    endif()
    add_library(WorldStateRepository persistence/WorldStateRepository.cpp)
    target_include_directories(WorldStateRepository PUBLIC
        persistence
        persistence/sqlite_amalgamation)
    target_link_libraries(WorldStateRepository PUBLIC SQLiteAmalgamation)
else()
    add_library(WorldStateRepository persistence/WorldStateRepository.cpp)
    target_include_directories(WorldStateRepository PUBLIC persistence)
endif()

# World state snapshot + delta sync (PERSIST-3)
add_library(WorldStateSyncManager networking/WorldStateSyncManager.cpp)
target_include_directories(WorldStateSyncManager PUBLIC networking server)
target_link_libraries(WorldStateSyncManager PUBLIC GameServer)
find_package(lz4 QUIET)
if(lz4_FOUND)
    target_link_libraries(WorldStateSyncManager PUBLIC lz4::lz4)
    target_compile_definitions(WorldStateSyncManager PUBLIC NOVA_HAVE_LZ4=1)
else()
    find_library(LZ4_LIB NAMES lz4 liblz4)
    find_path(LZ4_INCLUDE_DIR lz4.h)
    if(LZ4_LIB AND LZ4_INCLUDE_DIR)
        target_include_directories(WorldStateSyncManager PUBLIC ${LZ4_INCLUDE_DIR})
        target_link_libraries(WorldStateSyncManager PUBLIC ${LZ4_LIB})
        target_compile_definitions(WorldStateSyncManager PUBLIC NOVA_HAVE_LZ4=1)
    endif()
endif()

# Animation blend spaces (M4)
add_library(BlendSpace1D animation/blending/BlendSpace1D.cpp)
target_include_directories(BlendSpace1D PUBLIC animation/blending)

# Lighting material functions (M5)
add_library(LightMaterialFunction lighting/LightMaterialFunction.cpp)
target_include_directories(LightMaterialFunction PUBLIC lighting core)

# Test executable
add_executable(nova_unit_tests tests/nova_unit_tests.cpp)
target_link_libraries(nova_unit_tests TextureImporter ModelImporter)

# BlendSpace1D unit tests (M4)
add_executable(nova_blend_space_1d_tests tests/test_blend_space_1d.cpp)
target_link_libraries(nova_blend_space_1d_tests PRIVATE BlendSpace1D)

# LightMaterialFunction serialization tests (M5)
add_executable(nova_light_material_function_tests tests/test_light_material_function.cpp)
target_link_libraries(nova_light_material_function_tests PRIVATE LightMaterialFunction)

# Particle system tests
add_executable(nova_particle_tests tests/test_particle_system.cpp)
target_include_directories(nova_particle_tests PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})

# GameServer tests
add_executable(nova_game_server_tests server/tests/test_game_server.cpp)
target_link_libraries(nova_game_server_tests PRIVATE GameServer)

# RestApi integration tests
add_executable(nova_rest_api_tests server/tests/test_rest_api.cpp)
target_link_libraries(nova_rest_api_tests PRIVATE RestApi)

# WorldStateRepository integration tests (PERSIST-1)
add_executable(nova_world_state_tests persistence/tests/test_world_state_repository.cpp)
target_link_libraries(nova_world_state_tests PRIVATE WorldStateRepository)

enable_testing()
add_test(NAME nova_unit_tests COMMAND nova_unit_tests)
add_test(NAME nova_particle_tests COMMAND nova_particle_tests)
add_test(NAME nova_game_server_tests COMMAND nova_game_server_tests)
add_test(NAME nova_rest_api_tests COMMAND nova_rest_api_tests)
add_test(NAME nova_world_state_tests COMMAND nova_world_state_tests)
add_test(NAME nova_blend_space_1d_tests COMMAND nova_blend_space_1d_tests)
add_test(NAME nova_light_material_function_tests COMMAND nova_light_material_function_tests)
