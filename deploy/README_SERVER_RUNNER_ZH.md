# DataPipeline 服务器 Runner 一次性初始化说明

本操作只需由服务器维护者执行一次。请使用 `xinzhi` 用户，不需要提供服务器密码，
不需要 tmux、Docker socket 或 root 权限，也不会删除现有 `DataPipeline/outputs` 数据。

## 执行前

应取得以下三个内容：

1. CI 发布的 `datapipeline-server-runner-bootstrap.tar.gz`；
2. 同一 Release 中的 `.sha256` 文件；
3. 单独通过安全渠道提供的临时 runner 注册令牌。

注册令牌不得上传到 Gitea Release、提交到 Git、写入聊天记录或直接出现在 shell 命令中。

## 核验和执行

在下载文件所在目录执行：

```bash
sha256sum -c datapipeline-server-runner-bootstrap.tar.gz.sha256

bootstrap_dir="$(mktemp -d /tmp/datapipeline-runner-bootstrap.XXXXXX)"
token_file="$(mktemp /tmp/datapipeline-runner-token.XXXXXX)"
chmod 0600 "$token_file"
trap 'rm -rf -- "$bootstrap_dir"; rm -f -- "$token_file"' EXIT

tar -xzf datapipeline-server-runner-bootstrap.tar.gz -C "$bootstrap_dir"
read -r -s -p "请输入临时 runner 注册令牌：" runner_token
printf '\n'
printf '%s' "$runner_token" > "$token_file"
unset runner_token

bash "$bootstrap_dir/deploy/bootstrap_server_runner.sh" \
  --token-file "$token_file" \
  --runner-binary "$bootstrap_dir/gitea-runner-3.1.0-linux-amd64" \
  --checksum-file "$bootstrap_dir/gitea-runner-checksums.txt" \
  --instance http://192.168.50.209:3000
```

脚本会自动识别 4131 端口当前对应的旧 QA Dashboard user-systemd 服务，并显示服务名、
PID 和启动参数。只有维护者在终端输入中文“确认”后才会继续。识别不唯一、端口程序不是
`qa_control_dashboard.py` 或服务归属不一致时，脚本会停止，不会关闭任何现有服务。

## 完成标准

```bash
systemctl --user is-active gitea-runner-datapipeline-prod.service
systemctl --user --no-pager --full status gitea-runner-datapipeline-prod.service
```

同时在 Gitea 仓库的 Actions Runner 页面确认：

- 名称：`datapipeline-prod-server`
- 标签：`datapipeline-prod`
- 状态：在线

确认在线后，删除下载的压缩包和 checksum 文件，并通知仓库管理员重置 runner 注册令牌。
此时尚未部署生产代码；只有受保护的 master 更新且测试通过后才会执行第一次部署。
