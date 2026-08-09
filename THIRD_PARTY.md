# 第三方依赖与参考说明

## 代码来源声明

本仓库的业务源码为本项目独立实现。当前仓库审查未发现从下列调研项目复制、改写或搬运的源码，也未将它们作为 Git 子模块或运行时依赖。因此，本仓库不包含需要继承这些项目版权头的代码。

项目规划与架构调研曾参考以下公开项目的产品思路和公开文档：

- [xliking/wechat_ai](https://github.com/xliking/wechat_ai)：参考其微信客服 AI 产品功能、命令体验和责任链式处理思路；未复制源码。
- [langbot-app/LangBot](https://github.com/langbot-app/LangBot)：参考其多模型、Pipeline、插件及管理平台的架构方向；未复制源码。
- [hanfangyuan4396/dify-on-wechat](https://github.com/hanfangyuan4396/dify-on-wechat)：参考其渠道抽象和聊天命令交互方式；未复制源码。
- [rcfcu2000/openclaw-wecom-kf](https://github.com/rcfcu2000/openclaw-wecom-kf)：仅研究微信客服插件的接口组织方向。审查时未确认明确开源许可证，因此尤其没有复制其代码。

“参考产品能力、公开协议和架构思想”不等同于复制受版权保护的实现；以上说明用于透明披露项目形成过程，而不是声明衍生关系或背书关系。

## 微信接口依据

微信客服协议实现依据企业微信官方公开 API 的参数和行为，包括回调加解密、`sync_msg`、`send_msg` 与 access token 接口。企业微信及微信相关名称和商标归其权利人所有，本项目与腾讯或企业微信不存在官方隶属或背书关系。

可选 ClawBot Bridge 的 iLink HTTP/JSON 契约依据腾讯发布的 npm 包
[`@tencent-weixin/openclaw-weixin` 2.4.6](https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin)。
该包声明为 MIT License，Copyright (C) 2026 Tencent；许可证原文副本保存在
[`bridge/third_party/tencent-openclaw-weixin-LICENSE`](bridge/third_party/tencent-openclaw-weixin-LICENSE)。
Bridge 使用相同的公开端点、请求头和 JSON 字段以实现协议互操作，但 FastAPI 服务、网关适配、
多实例状态、加密持久化、租户上下文映射与 Python 测试均由本项目独立实现。

早期调研曾阅读 `SiverKing/weixin-ClawBot-API`，但该仓库没有明确开源许可证。发布前已执行
源码相似性与人工来源审计；本仓库不包含该项目源码，也不以其为运行时依赖。协议层公开版本按
腾讯 MIT 包重新实现。该调研项目不属于本项目的授权来源。若后续修改协议层，维护者必须继续以
已明确授权的官方实现和公开协议行为为来源，不能从该无许可证仓库复制或移植实现。

## 软件包依赖

Python 与容器依赖通过 `pyproject.toml`、`uv.lock` 和基础镜像声明，由各自作者按各自许可证发布。本项目的 Apache-2.0 许可证仅覆盖本项目原创代码，不改变第三方依赖的许可证。

发布二进制或容器镜像时，维护者应根据实际打包版本生成软件物料清单（SBOM）和依赖许可证清单；依赖升级后应重新生成，不能把一份静态清单永久视为准确。
