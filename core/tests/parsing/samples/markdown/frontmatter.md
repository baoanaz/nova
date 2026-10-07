---
title: 认证设计
tags: [token, session]
---

# 认证设计

本文说明 `TokenService::refresh` 的实现位置，见 core/nova_core/types.py。

## 刷新时机

过期前 5 分钟刷新。
