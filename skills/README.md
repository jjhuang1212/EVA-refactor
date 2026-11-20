# skills

每个子目录是一个 skill，目录名就是 skill 名（小写字母、数字、`-`、`_`，字母开头）：

```
skills/<名字>/
  SKILL.md        frontmatter 写 name（必须等于目录名）和 description；正文写判断和步骤
  references/     可选。只放 .md。正文里讲不下的细节，模型用到哪个再用 load_skill 的 file 参数读哪个
  scripts/        可选。给 run_python 用的脚本，运行时放在工作目录的 skills/<名字>/ 下
```

三层按需披露：系统提示里只有名字和 description，读 SKILL.md 才有正文，正文提到的 references 用到才读。
正文写“什么时候用、怎么判断、主流程”，细节规则、字段说明、长示例放 references，并在正文里写明什么情况下读哪个。

启动时全部校验，任何一个不合格都会让服务启动失败。加载、校验和格式细节见 `eva/skills/loader.py`。
