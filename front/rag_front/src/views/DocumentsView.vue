<script setup lang="ts">
import { t } from '@/utils/locale'
import { graphStatusLabel } from '@/utils/graph'
import { Check, Plus, Refresh, Upload, Delete } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadFile } from 'element-plus'
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { buildGraph, fetchGraphStates } from '@/api/modules/graph'
import type { GraphState, DocumentItem } from '@/types/api'
import DocumentQuality from '@/components/DocumentQuality.vue'
import { notifyError } from '@/api/http'
import { notifyDocumentResult } from '@/utils/documentFeedback'
import { useDocumentStore } from '@/stores/documents'
import { formatDateTime, formatFileSize } from '@/utils/format'

const graphStates = ref<Record<string, GraphState>>({})
const selectedIds = ref<string[]>([])
const buildingGraph = ref(false)
let graphTimer: ReturnType<typeof setInterval> | undefined
async function refreshGraphStates() {
  try {
    const all: Record<string, GraphState> = {}
    for (let offset = 0; ; offset += 100) {
      const response = await fetchGraphStates(offset)
      response.items.forEach(s => { all[s.document_id] = s })
      if (response.items.length < 100) break
    }
    graphStates.value = all
  } catch (error) { notifyError(error, t('图谱状态加载失败', 'Could not load graph status')) }
}
async function submitGraph(ids: string[]) {
  if (!ids.length) { ElMessage.warning(t('请先选择文档', 'Select a document first')); return }
  buildingGraph.value = true
  try {
    for (let i = 0; i < ids.length; i += 100) await buildGraph(ids.slice(i, i + 100))
    ElMessage.success(t('建图任务已提交，可在知识图谱页面查看进度', 'Graph build queued. View progress on the Knowledge Graph page.'))
    await refreshGraphStates()
  } catch (error) { notifyError(error, t('建图任务提交失败', 'Could not submit the graph build')) }
  finally { buildingGraph.value = false }
}
const router = useRouter()
const documentStore = useDocumentStore()

const textDialogVisible = ref(false)
const uploadDialogVisible = ref(false)
const rebuildingDocumentId = ref('')

const textForm = reactive({
  filename: '',
  knowledge_base: 'default',
  preferred_splitter: '',
  content: '',
})

const uploadForm = reactive({
  knowledge_base: 'default',
  preferred_splitter: '',
  file: null as File | null,
})

const splitterOptions = computed(() => documentStore.splitters)

function handleUploadChange(uploadFile: UploadFile) {
  uploadForm.file = uploadFile.raw || null
}

async function submitTextDocument() {
  try {
    const document = await documentStore.createTextDocument({
      filename: textForm.filename,
      knowledge_base: textForm.knowledge_base,
      preferred_splitter: textForm.preferred_splitter || null,
      content: textForm.content,
    })
    notifyDocumentResult(document.summary, t('文本已完成入库', 'Text imported'))
    textDialogVisible.value = false
    Object.assign(textForm, {
      filename: '',
      knowledge_base: 'default',
      preferred_splitter: '',
      content: '',
    })
  } catch (error) {
    notifyError(error, t('文本入库失败', 'Could not import the text'))
  }
}

async function submitUploadDocument() {
  if (!uploadForm.file) {
    ElMessage.warning(t('请先选择文件', 'Choose a file first'))
    return
  }

  try {
    const document = await documentStore.createUploadDocument({
      file: uploadForm.file,
      knowledge_base: uploadForm.knowledge_base,
      preferred_splitter: uploadForm.preferred_splitter || null,
    })
    notifyDocumentResult(document.summary, t('文件已上传并完成入库', 'File uploaded and indexed'))
    uploadDialogVisible.value = false
    Object.assign(uploadForm, {
      knowledge_base: 'default',
      preferred_splitter: '',
      file: null,
    })
  } catch (error) {
    notifyError(error, t('文件上传失败', 'Could not upload the file'))
  }
}

async function handleRebuild(documentId: string) {
  rebuildingDocumentId.value = documentId
  try {
    const document = await documentStore.triggerRebuild(documentId)
    notifyDocumentResult(document.summary, t('文档索引重建完成', 'Document index rebuilt'))
  } catch (error) {
    notifyError(error, t('重建索引失败', 'Could not rebuild the index'))
  } finally {
    rebuildingDocumentId.value = ''
  }
}

async function handleDeleteDocument(documentId: string) {
  try {
    await ElMessageBox.confirm(
      t('删除文档将级联删除其所有的 Chunk 记录及 Milvus 向量，并且会尝试删除物理源文件，操作不可逆。确定继续？', 'Permanently delete this document, its chunks, and its vectors? The original upload will also be removed if available.'),
      t('彻底删除确认', 'Permanently delete document'),
      {
        type: 'error',
        confirmButtonText: t('确认彻底删除', 'Delete permanently'),
        cancelButtonText: t('取消', 'Cancel'),
      }
    )
    await documentStore.removeDocument(documentId)
    ElMessage.success(t('文档已彻底删除', 'Document deleted'))
  } catch (error) {
    if (error !== 'cancel') {
      notifyError(error, t('删除文档失败', 'Could not delete the document'))
    }
  }
}

onMounted(async () => {
  await documentStore.initialize()
  await refreshGraphStates()
  graphTimer = setInterval(() => { void refreshGraphStates() }, 5000)
})
onUnmounted(() => clearInterval(graphTimer))
</script>

<template>
  <div class="page-grid">
    <section class="panel span-12 toolbar-card">
      <div>
        <div class="panel-kicker">Ingestion Pipeline</div>
        <h2>{{ t('文档入库与索引重建', 'Document import and indexing') }}</h2>
        <p class="page-description">{{ t('支持文件上传、纯文本导入、切分策略选择与索引重建，所有结果都会同步写入文档列表和 Chunk 管理页。', 'Upload files or import text, select a chunking strategy, and rebuild indexes.') }}</p>
      </div>
      <div class="toolbar-actions">
        <el-button :loading="buildingGraph" @click="submitGraph(selectedIds)">{{ t('批量构建图谱', 'Build selected graphs') }}</el-button>
        <el-button :icon="Refresh" @click="documentStore.initialize()">{{ t('刷新列表', 'Refresh list') }}</el-button>
        <el-button :icon="Plus" @click="textDialogVisible = true">{{ t('纯文本入库', 'Import text') }}</el-button>
        <el-button type="primary" :icon="Upload" @click="uploadDialogVisible = true">{{ t('上传文件', 'Upload file') }}</el-button>
      </div>
    </section>

    <section class="panel span-12 section-card">
      <div class="section-heading">
        <div>
          <div class="panel-kicker">Documents</div>
          <h3>{{ t('知识文档列表', 'Knowledge documents') }}</h3>
        </div>
        <el-tag effect="dark">{{ documentStore.documents.length }} {{ t('份文档', 'documents') }}</el-tag>
      </div>

      <el-table v-loading="documentStore.loading" :data="documentStore.documents" stripe @selection-change="(rows: DocumentItem[]) => selectedIds = rows.map(r => r.id)">
        <el-table-column type="selection" width="45" />
        <el-table-column :label="t('图谱状态', 'Graph status')" width="120"><template #default="{ row }"><el-tag>{{ graphStatusLabel(graphStates[row.id]?.status) }}</el-tag></template></el-table-column>
        <el-table-column prop="filename" :label="t('文件名', 'File name')" min-width="240" />
        <el-table-column :label="t('解析质量', 'Parse quality')" min-width="180">
          <template #default="scope"><DocumentQuality :summary="scope.row.summary" compact /></template>
        </el-table-column>
        <el-table-column prop="knowledge_base" :label="t('知识库', 'Knowledge base')" min-width="120" />
        <el-table-column prop="file_type" :label="t('类型', 'Type')" width="100" />
        <el-table-column prop="chunk_count" label="Chunks" width="100" />
        <el-table-column :label="t('大小', 'Size')" width="120">
          <template #default="scope">{{ formatFileSize(scope.row.file_size) }}</template>
        </el-table-column>
        <el-table-column :label="t('更新时间', 'Updated at')" min-width="150">
          <template #default="scope">{{ formatDateTime(scope.row.updated_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('操作', 'Actions')" width="500" fixed="right">
          <template #default="scope">
            <div class="inline-actions">
              <el-button size="small" :loading="buildingGraph" @click="submitGraph([scope.row.id])">{{ t('构建 / 重试', 'Build or retry') }}</el-button>
              <el-button size="small" @click="router.push({ name: 'graph', query: { documentId: scope.row.id, knowledgeBase: scope.row.knowledge_base } })">{{ t('查看图谱', 'View graph') }}</el-button>
              <el-button size="small" @click="router.push({ name: 'chunks', query: { documentId: scope.row.id } })">{{ t('查看切分', 'View chunks') }}</el-button>
              <el-button size="small" @click="router.push({ name: 'retrieval', query: { keyword: scope.row.filename } })">{{ t('检索调试', 'Retrieval Console') }}</el-button>
              <el-button
                size="small"
                :loading="rebuildingDocumentId === scope.row.id"
                @click="handleRebuild(scope.row.id)"
              >
                {{ t('重建索引', 'Rebuild index') }}
              </el-button>
              <el-button
                type="danger"
                plain
                size="small"
                @click="handleDeleteDocument(scope.row.id)"
              >
                {{ t('删除', 'Delete') }}
              </el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <el-dialog v-model="textDialogVisible" :title="t('纯文本快速入库', 'Import plain text')" width="720">
      <el-form label-position="top">
        <el-form-item :label="t('文件名', 'File name')">
          <el-input v-model="textForm.filename" :placeholder="t('例如：rule.md', 'Example: rule.md')" />
        </el-form-item>
        <div class="form-grid two-columns">
          <el-form-item :label="t('知识库名称', 'Knowledge base')">
            <el-input v-model="textForm.knowledge_base" />
          </el-form-item>
          <el-form-item :label="t('切分策略', 'Chunking strategy')">
            <el-select v-model="textForm.preferred_splitter" clearable :placeholder="t('自动判断', 'Auto')">
              <el-option v-for="item in splitterOptions" :key="item.name" :label="item.name" :value="item.name" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item :label="t('正文内容', 'Content')">
          <el-input v-model="textForm.content" type="textarea" :rows="14" :placeholder="t('输入要入库的完整文本内容', 'Enter the full text to import')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="textDialogVisible = false">{{ t('取消', 'Cancel') }}</el-button>
        <el-button type="primary" :icon="Check" @click="submitTextDocument">{{ t('开始入库', 'Import text') }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="uploadDialogVisible" :title="t('上传文件并入库', 'Upload and index a file')" width="620">
      <el-form label-position="top">
        <div class="form-grid two-columns">
          <el-form-item :label="t('知识库名称', 'Knowledge base')">
            <el-input v-model="uploadForm.knowledge_base" />
          </el-form-item>
          <el-form-item :label="t('切分策略', 'Chunking strategy')">
            <el-select v-model="uploadForm.preferred_splitter" clearable :placeholder="t('自动判断', 'Auto')">
              <el-option v-for="item in splitterOptions" :key="item.name" :label="item.name" :value="item.name" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item :label="t('文件选择', 'File')">
          <el-upload
            drag
            :auto-upload="false"
            :show-file-list="true"
            :limit="1"
            :on-change="handleUploadChange"
            :on-remove="() => { uploadForm.file = null }"
          >
            <el-icon class="el-icon--upload"><Upload /></el-icon>
            <div class="el-upload__text">{{ t('拖拽文件到此处，或', 'Drop a file here or') }} <em>{{ t('点击选择文件', 'choose a file') }}</em></div>
            <template #tip>
              <div class="upload-tip">{{ t('支持 txt / md / pdf / docx，上传后自动解析、切分和写入向量索引。', 'Supports TXT, Markdown, PDF, and DOCX. Files are parsed, chunked, and indexed.') }}</div>
            </template>
          </el-upload>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="uploadDialogVisible = false">{{ t('取消', 'Cancel') }}</el-button>
        <el-button type="primary" :icon="Upload" @click="submitUploadDocument">{{ t('上传并入库', 'Upload and index') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>
