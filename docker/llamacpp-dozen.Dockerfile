# llama.cpp server with Vulkan running on Mesa "Dozen" (Vulkan on Direct3D 12).
#
# Docker Desktop on Windows only passes NVIDIA GPUs to containers - but WSL2 exposes EVERY GPU
# (AMD, NVIDIA, Intel) as /dev/dxg (DirectX). Dozen turns that into Vulkan, and llama.cpp's Vulkan
# backend runs the Qwen coder on it. Run with:
#   --device /dev/dxg  -v /usr/lib/wsl:/usr/lib/wsl     (see docker/compose.llamacpp-dxg.yml)
FROM archlinux:latest AS build
RUN pacman -Syu --noconfirm --needed base-devel git cmake ninja curl openssl \
      vulkan-headers vulkan-icd-loader shaderc spirv-headers glslang && pacman -Scc --noconfirm
ARG LLAMA_CPP_REF=master
RUN git clone --depth 1 --branch "${LLAMA_CPP_REF}" https://github.com/ggml-org/llama.cpp /src \
 && cmake -S /src -B /build -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_VULKAN=ON -DGGML_NATIVE=OFF \
      -DLLAMA_CURL=ON -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
 && cmake --build /build --target llama-server -j

FROM archlinux:latest
RUN pacman -Syu --noconfirm --needed vulkan-icd-loader vulkan-dzn curl openssl gcc-libs && pacman -Scc --noconfirm
COPY --from=build /build/bin/ /app/
ENV LD_LIBRARY_PATH=/usr/lib/wsl/lib:/app \
    MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE=1 \
    LLAMA_ARG_HOST=0.0.0.0 \
    LLAMA_ARG_PORT=8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=40 CMD curl -fs http://127.0.0.1:8000/health || exit 1
ENTRYPOINT ["/app/llama-server"]
