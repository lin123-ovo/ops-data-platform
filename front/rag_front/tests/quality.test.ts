import assert from 'node:assert/strict'
import test from 'node:test'
import { readQualityReport } from '../src/utils/quality.ts'

test('旧记录和缺失报告显示未检测', () => {
  for (const summary of [null, undefined, '', 'parser=old; splitter=auto']) {
    assert.deepEqual(readQualityReport(summary), { checked: false, warnings: [] })
  }
})

test('正常报告与中文警告正确读取', () => {
  for (const warnings of [[], ['发现 1 个替代字符（�），请核对原文。']]) {
    const report = { checked: true, warnings }
    assert.deepEqual(readQualityReport(`parser=test\nquality_report_v1=${JSON.stringify(report)}`), report)
  }
})

test('损坏或类型错误的报告不会中断页面', () => {
  for (const payload of ['broken', 'null', '[]', '{}', '{"checked":1,"warnings":[]}', '{"checked":true,"warnings":[1]}']) {
    assert.deepEqual(readQualityReport(`quality_report_v1=${payload}`), { checked: false, warnings: [] })
  }
})

test('读取最后一条报告且不解析正文内的类似标记', () => {
  assert.deepEqual(readQualityReport('parser=test quality_report_v1=broken'), { checked: false, warnings: [] })
  assert.deepEqual(readQualityReport('quality_report_v1=broken\nquality_report_v1={"checked":true,"warnings":[]}'),
    { checked: true, warnings: [] })
})
