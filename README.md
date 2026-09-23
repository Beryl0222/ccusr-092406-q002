# 学校体育实课运行

核对体育课程、场地教师、实际授课和未成年人数据的合规使用。

`fixtures/domain.json` 保存首批领域名词和状态样例，便于接口联调时保持一致语义。运行 `python3 service.py --check` 可检查基础配置；执行 `npm test` 可验证健康入口与领域规则。

## 领域核心

`pe_domain/` 实现带版本的学期方案、教师/场地/抽样学生三方最小化交叉确认、运动时长防虚增、
降雨高温替代与伤病适配、“教会、勤练、常赛”覆盖规则、教研复核闭环、事件账本实况还原，
以及公众聚合/家长本人视图的分级可见性。设计说明见 [`docs/design.md`](docs/design.md)，
规则的可执行示例见 `test_pe_domain.py`。

## 本地运行

测试命令：

```bash
python3 -m unittest discover -s . -p 'test_*.py' && npm test
```

编译或构建命令：

```bash
python3 -m compileall -q .
```

所有测试和构建均在单个 Linux 应用容器内完成，不需要另行启动数据库或外部服务。
现有自动化测试描述当前基线行为，修复工作应在保持其余业务规则的前提下补充回归场景。
