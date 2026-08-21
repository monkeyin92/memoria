# P3-1 完善构建供应链门禁

## 目标

建立完整的软件供应链安全体系，确保所有构建产物的来源可追溯、依赖可验证，防止供应链攻击。

## 背景

根据整改方案第 21.1 节：
> 供应链门禁可与上述工作并行补齐，但必须覆盖当前 Go、Python、C++、ESP-IDF、容器、代码生成和第三方 Action 链路；不因为 Rust 供应链事件新增 Rust。

现代软件供应链攻击频发，必须建立多层防护。

## 实施方案

### 1. 依赖锁定与验证

#### 1.1 Go 模块

**文件**: `go.sum`

```bash
# 使用 go mod 锁定依赖
go mod tidy
go mod verify

# 生成 SBOM
go install github.com/anchore/syft/cmd/syft@latest
syft packages . -o spdx-json > sbom-go.json
```

**门禁检查**:
```yaml
# .github/workflows/go-supply-chain.yml
name: Go Supply Chain Check

on: [push, pull_request]

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Setup Go
        uses: actions/setup-go@v4
        with:
          go-version: '1.21'
      
      - name: Verify go.sum
        run: go mod verify
      
      - name: Check for known vulnerabilities
        run: |
          go install golang.org/x/vuln/cmd/govulncheck@latest
          govulncheck ./...
      
      - name: Generate SBOM
        run: |
          go install github.com/anchore/syft/cmd/syft@latest
          syft packages . -o spdx-json > sbom-go.json
      
      - name: Upload SBOM
        uses: actions/upload-artifact@v3
        with:
          name: sbom-go
          path: sbom-go.json
```

#### 1.2 Python 依赖

**文件**: `requirements.lock`

```bash
# 使用 pip-tools 锁定依赖
pip install pip-tools
pip-compile requirements.in --generate-hashes --output-file requirements.lock

# 验证安装
pip-sync requirements.lock
```

**门禁检查**:
```yaml
# .github/workflows/python-supply-chain.yml
name: Python Supply Chain Check

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Setup Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      
      - name: Verify requirements.lock
        run: |
          pip install pip-tools
          pip-compile requirements.in --generate-hashes --dry-run
      
      - name: Check for vulnerabilities
        run: |
          pip install safety
          safety check -r requirements.lock --json
      
      - name: Generate SBOM
        run: |
          pip install cyclonedx-bom
          cyclonedx-py -r -i requirements.lock -o sbom-python.json
```

#### 1.3 ESP-IDF 组件

**文件**: `firmware/esp32/idf_component.yml`

```yaml
dependencies:
  espressif/esp-sr:
    version: "^1.4.0"
    rules:
      - if: "idf_version >= 5.0"
  espressif/esp-dsp:
    version: "1.4.11"
```

**门禁检查**:
```bash
# 验证组件
cd firmware/esp32
idf.py reconfigure

# 检查已安装组件
cat managed_components/*/idf_component.yml

# 生成组件清单
idf.py show_manifest
```

#### 1.4 容器基础镜像

**文件**: `services/*/Dockerfile`

```dockerfile
# 使用 digest 固定版本（不使用 latest）
FROM python:3.11.9-slim@sha256:abc123...

# 验证签名（如果支持）
# COPY --from=docker.io/library/python:3.11.9-slim@sha256:abc123... /usr/local /usr/local

# 扫描漏洞
# RUN apt-get update && apt-get upgrade -y
```

**门禁检查**:
```yaml
# .github/workflows/container-supply-chain.yml
name: Container Supply Chain Check

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Build image
        run: docker build -t memoria-agent:test services/agent
      
      - name: Scan with Trivy
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: memoria-agent:test
          format: 'sarif'
          output: 'trivy-results.sarif'
      
      - name: Upload scan results
        uses: github/codeql-action/upload-sarif@v2
        with:
          sarif_file: 'trivy-results.sarif'
```

### 2. 构建可重现性

#### 2.1 固定构建环境

**文件**: `.github/workflows/build.yml`

```yaml
name: Reproducible Build

on: [push, pull_request]

jobs:
  build:
    runs-on: ubuntu-22.04  # 固定版本
    
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # 完整历史，用于版本标记
      
      - name: Setup build env
        run: |
          # 固定工具链版本
          echo "BUILD_DATE=$(date -u +'%Y%m%d-%H%M%S')" >> $GITHUB_ENV
          echo "GIT_COMMIT=$(git rev-parse HEAD)" >> $GITHUB_ENV
          echo "GIT_DIRTY=$(git diff --quiet || echo '-dirty')" >> $GITHUB_ENV
      
      - name: Build with fixed versions
        run: |
          # Go 构建
          go build -ldflags="-X main.Version=${GIT_COMMIT}${GIT_DIRTY} -X main.BuildDate=${BUILD_DATE}" ./services/media_edge
          
          # Python 镜像构建
          docker build \
            --build-arg BUILD_DATE=${BUILD_DATE} \
            --build-arg GIT_COMMIT=${GIT_COMMIT} \
            -t memoria-agent:${GIT_COMMIT} \
            services/agent
      
      - name: Generate build attestation
        run: |
          # 记录构建环境
          cat > build-attestation.json <<EOF
          {
            "builder": {
              "runner": "github-actions",
              "os": "ubuntu-22.04",
              "timestamp": "$(date -u --iso-8601=seconds)"
            },
            "source": {
              "repo": "${GITHUB_REPOSITORY}",
              "commit": "${GIT_COMMIT}",
              "ref": "${GITHUB_REF}"
            },
            "artifacts": [
              {
                "name": "memoria-agent",
                "digest": "$(docker images --no-trunc --quiet memoria-agent:${GIT_COMMIT})"
              }
            ]
          }
          EOF
      
      - name: Upload attestation
        uses: actions/upload-artifact@v3
        with:
          name: build-attestation
          path: build-attestation.json
```

#### 2.2 固件构建

**文件**: `firmware/esp32/build.sh`

```bash
#!/bin/bash
set -euo pipefail

# 固定 ESP-IDF 版本
export IDF_VERSION="v5.1.2"
export IDF_PATH="/opt/esp/idf"

# 记录构建信息
BUILD_DATE=$(date -u +'%Y%m%d-%H%M%S')
GIT_COMMIT=$(git rev-parse HEAD)

echo "Building firmware: ${BUILD_DATE}-${GIT_COMMIT}"

# 清理旧构建
idf.py fullclean

# 配置
idf.py set-target esp32s3

# 构建
idf.py build

# 生成清单
cat > build/manifest.json <<EOF
{
  "firmware_version": "${BUILD_DATE}-${GIT_COMMIT}",
  "idf_version": "${IDF_VERSION}",
  "target": "esp32s3",
  "build_date": "$(date -u --iso-8601=seconds)",
  "artifacts": {
    "bootloader": "$(sha256sum build/bootloader/bootloader.bin | cut -d' ' -f1)",
    "partition_table": "$(sha256sum build/partition_table/partition-table.bin | cut -d' ' -f1)",
    "app": "$(sha256sum build/memoria.bin | cut -d' ' -f1)"
  }
}
EOF

echo "Build manifest:"
cat build/manifest.json
```

### 3. 第三方 Action 审计

#### 3.1 固定 Action 版本

**反例**（不安全）:
```yaml
- uses: actions/checkout@main  # ❌ 使用浮动分支
- uses: some-org/action@v1     # ❌ 使用浮动标签
```

**正例**（安全）:
```yaml
- uses: actions/checkout@v4.1.1  # ✅ 固定语义版本
  # 或使用 commit SHA（更安全）
- uses: actions/checkout@8ade135a41bc03ea155e62e844d188df1ea18608  # v4.1.1
```

#### 3.2 Action 白名单

**文件**: `.github/workflows/action-audit.yml`

```yaml
name: Audit Third-Party Actions

on: [pull_request]

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Check for unapproved actions
        run: |
          # 允许的 Action 白名单
          ALLOWED_ACTIONS=(
            "actions/checkout@v4"
            "actions/setup-go@v4"
            "actions/setup-python@v4"
            "actions/upload-artifact@v3"
            "docker/build-push-action@v5"
            "aquasecurity/trivy-action@master"
          )
          
          # 扫描所有 workflow 文件
          for workflow in .github/workflows/*.yml; do
            echo "Checking $workflow..."
            
            # 提取使用的 actions
            grep -oP 'uses:\s*\K[^@]+@[^\s]+' "$workflow" | while read action; do
              allowed=false
              
              for allowed_action in "${ALLOWED_ACTIONS[@]}"; do
                if [[ "$action" == "$allowed_action"* ]]; then
                  allowed=true
                  break
                fi
              done
              
              if [[ "$allowed" == false ]]; then
                echo "❌ Unapproved action: $action in $workflow"
                exit 1
              fi
            done
          done
          
          echo "✅ All actions are approved"
```

### 4. 代码生成器安全

#### 4.1 Protobuf 编译

**固定 protoc 版本**:
```bash
# 使用固定版本的 protoc
PROTOC_VERSION="25.1"
curl -LO "https://github.com/protocolbuffers/protobuf/releases/download/v${PROTOC_VERSION}/protoc-${PROTOC_VERSION}-linux-x86_64.zip"
echo "expected-sha256  protoc-${PROTOC_VERSION}-linux-x86_64.zip" | sha256sum -c
```

**验证生成代码**:
```bash
# 重新生成并对比
make proto-gen
git diff --exit-code packages/proto/

# 如果有差异，构建失败
if [ $? -ne 0 ]; then
  echo "❌ Generated proto files differ from committed files"
  exit 1
fi
```

#### 4.2 OpenAPI 代码生成

```bash
# 固定 openapi-generator 版本
OPENAPI_GEN_VERSION="7.2.0"
docker run --rm \
  -v ${PWD}:/local \
  openapitools/openapi-generator-cli:v${OPENAPI_GEN_VERSION} \
  generate \
  -i /local/api/openapi.yaml \
  -g python \
  -o /local/generated/python
```

### 5. Secret 扫描

#### 5.1 预提交检查

**文件**: `.pre-commit-config.yaml`

```yaml
repos:
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.4.0
    hooks:
      - id: detect-secrets
        args: ['--baseline', '.secrets.baseline']
  
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.18.0
    hooks:
      - id: gitleaks
```

**安装**:
```bash
pip install pre-commit
pre-commit install
```

#### 5.2 CI 扫描

```yaml
# .github/workflows/secret-scan.yml
name: Secret Scanning

on: [push, pull_request]

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      
      - name: Run Gitleaks
        uses: gitleaks/gitleaks-action@v2
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### 6. 固件签名

#### 6.1 签名密钥管理

```bash
# 生成签名密钥（仅一次，安全存储）
espsecure.py generate_signing_key --version 2 secure_boot_signing_key.pem

# 永久备份，密钥不进入代码仓库
# 存储在硬件安全模块或密钥管理服务
```

#### 6.2 固件签名流程

```bash
# 配置 Secure Boot
idf.py menuconfig
# Security features → Enable hardware Secure Boot v2

# 构建并签名
idf.py build
espsecure.py sign_data --version 2 \
  --keyfile secure_boot_signing_key.pem \
  --output build/memoria-signed.bin \
  build/memoria.bin

# 验证签名
espsecure.py verify_signature --version 2 \
  --keyfile secure_boot_signing_key_pub.pem \
  build/memoria-signed.bin
```

### 7. SBOM 生成与发布

#### 7.1 统一 SBOM

**脚本**: `scripts/generate_sbom.sh`

```bash
#!/bin/bash

# 生成各组件 SBOM
echo "Generating SBOM for all components..."

# Go
syft packages services/media_edge -o spdx-json > sbom/media-edge.json

# Python
cyclonedx-py -r -i services/agent/requirements.lock -o sbom/agent.json

# Firmware
# TODO: ESP-IDF SBOM 生成

# 合并
jq -s '.' sbom/*.json > sbom/memoria-full.json

echo "SBOM generated: sbom/memoria-full.json"
```

#### 7.2 发布 SBOM

```yaml
# Release 时上传 SBOM
- name: Upload SBOM to release
  uses: actions/upload-release-asset@v1
  with:
    upload_url: ${{ steps.create_release.outputs.upload_url }}
    asset_path: sbom/memoria-full.json
    asset_name: memoria-sbom-${{ github.ref_name }}.json
    asset_content_type: application/json
```

## 验收标准

### 依赖管理

✅ Go 依赖锁定在 go.sum，每次构建验证  
✅ Python 依赖使用 hash 模式锁定  
✅ ESP-IDF 组件版本固定  
✅ 容器基础镜像使用 digest  

### 构建可重现性

✅ 相同源代码 + 相同环境 = 相同产物  
✅ 构建元数据（commit、日期）嵌入产物  
✅ 每次构建生成 attestation  

### 漏洞扫描

✅ Go 使用 govulncheck  
✅ Python 使用 safety  
✅ 容器使用 Trivy  
✅ 发现高危漏洞阻止发布  

### Secret 保护

✅ 预提交检查阻止 secret 提交  
✅ CI 扫描整个历史记录  
✅ 密钥不进入代码仓库  

### 固件安全

✅ Secure Boot v2 启用  
✅ 固件签名验证  
✅ 签名密钥硬件保护  

### SBOM

✅ 每次发布生成完整 SBOM  
✅ SBOM 包含所有依赖和版本  
✅ SBOM 随产物发布  

## 参考

- 整改方案第 15.9 节：运维与安全
- 整改方案第 21.1 节：执行顺序
- SLSA Framework: https://slsa.dev/
- ESP32 Secure Boot: https://docs.espressif.com/projects/esp-idf/en/latest/esp32/security/secure-boot-v2.html
