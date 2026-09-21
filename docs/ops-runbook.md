# Memoria 运维空间治理

所有清理脚本默认 dry-run；生产数据卷、非 Memoria 镜像和运行容器引用的镜像不在自动清理范围内。

## 镜像保留

```bash
sudo /opt/memoria/current/scripts/docker_image_retention.sh --keep 2 --min-age-days 14
sudo /opt/memoria/current/scripts/docker_image_retention.sh --keep 2 --min-age-days 14 --apply
```

执行 apply 前应保存 `docker ps` 与 `docker volume ls` 快照。工具只处理 `memoria-*` tagged images，不使用 `docker system prune`。

## 生产目录

```bash
sudo /opt/memoria/current/scripts/production_layout_audit.sh /opt/memoria
```

当前只做审计，不自动搬迁 `releases/`、`component-releases/` 或 `incoming/`，避免破坏 compose 引用链。

## 验收产物

```bash
python scripts/archive_acceptance_outputs.py --older-than-days 30 --keep 5
python scripts/archive_acceptance_outputs.py --older-than-days 30 --keep 5 --apply
```

范围仅为 `outputs/acceptance/run-*`；归档后校验文件哈希，再删除原目录。

## BuildKit

```bash
scripts/docker_build.sh -f infra/Dockerfile.media-edge -t memoria-media-edge:local .
MEMORIA_DOCKER_BUILDKIT=0 scripts/docker_build.sh -f infra/Dockerfile.media-edge -t memoria-media-edge:legacy .
```

生产机当前没有 buildx 插件；远端 Agent 快速发布默认保持 `DOCKER_BUILDKIT=0`，安装并灰度验证前不切换生产构建器。buildx 模式：
`MEMORIA_DOCKER_BUILDER=buildx scripts/docker_build.sh --load ...`。

## 磁盘巡检

```bash
scripts/disk_patrol.sh --warn-pct 75 --crit-pct 85 --json
sudo install -m 0644 infra/memoria-disk-patrol.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now memoria-disk-patrol.timer
```

巡检只读取 `df`/Docker 空间并在 `--apply` 时写快照，不执行清理。退出码：0 正常、1 warning、2 critical、3 自身错误。
