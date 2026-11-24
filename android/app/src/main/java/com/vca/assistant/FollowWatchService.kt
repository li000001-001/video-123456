package com.vca.assistant

import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioPlaybackCaptureConfiguration
import android.media.AudioRecord
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import com.google.android.gms.tasks.Tasks
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.label.ImageLabeler
import com.google.mlkit.vision.label.ImageLabeling
import com.google.mlkit.vision.label.defaults.ImageLabelerOptions
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.TextRecognizer
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/** 用户手动开启的 60 秒跟看：端侧 OCR/图像标签，原始帧不落盘。 */
class FollowWatchService : Service() {

    companion object {
        const val ACTION_START = "com.vca.assistant.action.START_WATCH"
        const val ACTION_STOP = "com.vca.assistant.action.STOP_WATCH"
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_PROJECTION_DATA = "projection_data"
        const val EXTRA_AUDIO_ALLOWED = "audio_allowed"

        private const val DURATION_SECONDS = 60
        private const val SAMPLE_INTERVAL_MS = 5_000L
        private const val PROGRESS_NOTIFICATION = 3001
        private const val RESULT_NOTIFICATION = 3002
        private const val PROGRESS_CHANNEL = "vca_watch"
        private const val RESULT_CHANNEL = "vca_watch_result"
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private val notificationManager by lazy { getSystemService(NotificationManager::class.java) }
    private val capturing = AtomicBoolean(false)
    private val degradations = ConcurrentHashMap.newKeySet<String>()
    private val ocrSamples = linkedSetOf<String>()
    private val visualSamples = linkedSetOf<String>()
    private val frameDigests = mutableSetOf<String>()

    @Volatile
    private var alive = true
    private var sessionActive = false
    private var audioAllowed = false
    private var startedAt = 0L
    private var sampleCount = 0
    private var blackFrames = 0
    private var trackedPackage = ""
    private var watchTitle = ""
    private var audioNonZero = false

    private var worker: HandlerThread? = null
    private var workerHandler: Handler? = null
    private var projection: MediaProjection? = null
    private var virtualDisplay: android.hardware.display.VirtualDisplay? = null
    private var imageReader: ImageReader? = null
    private var audioRecord: AudioRecord? = null
    private var audioThread: Thread? = null
    private var pcmFile: File? = null
    private var wavFile: File? = null
    private var uploadThread: Thread? = null

    @Volatile
    private var uploadConnection: HttpURLConnection? = null

    private lateinit var textRecognizer: TextRecognizer
    private lateinit var imageLabeler: ImageLabeler

    private val projectionCallback = object : MediaProjection.Callback() {
        override fun onStop() {
            if (capturing.get()) {
                degradations.add("录屏授权被系统或用户停止")
                finishCapture()
            }
        }
    }

    private val countdownRunnable = object : Runnable {
        override fun run() {
            if (!capturing.get()) return
            val elapsed = SystemClock.elapsedRealtime() - startedAt
            val left = ((DURATION_SECONDS * 1000L - elapsed + 999) / 1000).coerceAtLeast(0)
            updateProgress("跟看采集中 · 剩余 $left 秒")
            if (elapsed >= DURATION_SECONDS * 1000L) {
                finishCapture()
            } else {
                mainHandler.postDelayed(this, 1000)
            }
        }
    }

    private val sampleRunnable = object : Runnable {
        override fun run() {
            if (!capturing.get()) return
            sampleFrame()
            if (capturing.get()) workerHandler?.postDelayed(this, SAMPLE_INTERVAL_MS)
        }
    }

    override fun onCreate() {
        super.onCreate()
        createChannels()
        runCatching {
            textRecognizer = TextRecognition.getClient(
                ChineseTextRecognizerOptions.Builder().build()
            )
        }.onFailure { degradations.add("中文 OCR 初始化失败，已跳过画面文字") }
        runCatching {
            imageLabeler = ImageLabeling.getClient(ImageLabelerOptions.DEFAULT_OPTIONS)
        }.onFailure { degradations.add("图像标签不可用，已跳过") }
    }

    @Suppress("DEPRECATION")
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            if (capturing.get()) {
                degradations.add("用户提前停止跟看")
                finishCapture()
            } else {
                alive = false
                stopSelf()
            }
            return START_NOT_STICKY
        }
        if (intent?.action != ACTION_START || sessionActive) return START_NOT_STICKY
        sessionActive = true
        audioAllowed = intent.getBooleanExtra(EXTRA_AUDIO_ALLOWED, false)
        startForegroundProgress()

        val data = intent.getParcelableExtra<Intent>(EXTRA_PROJECTION_DATA)
        val resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, Activity.RESULT_CANCELED)
        if (data == null || resultCode != Activity.RESULT_OK) {
            failStart("缺少有效录屏授权")
            return START_NOT_STICKY
        }

        try {
            worker = HandlerThread("vca-watch-frames").also { it.start() }
            workerHandler = Handler(worker!!.looper)
            val manager = getSystemService(MediaProjectionManager::class.java)
            projection = manager.getMediaProjection(resultCode, data)
            val mediaProjection = projection ?: throw IllegalStateException("无法创建录屏会话")
            mediaProjection.registerCallback(projectionCallback, workerHandler)
            createVirtualDisplay(mediaProjection)
            capturing.set(true)
            startedAt = SystemClock.elapsedRealtime()
            startAudio(mediaProjection)
            workerHandler?.postDelayed(sampleRunnable, 3000)
            mainHandler.post(countdownRunnable)
            updateProgress("请返回视频 App · 剩余 $DURATION_SECONDS 秒")
        } catch (e: Exception) {
            failStart(e.message ?: "录屏或内录初始化失败")
        }
        return START_NOT_STICKY
    }

    private fun startForegroundProgress() {
        if (Build.VERSION.SDK_INT >= 29) {
            var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
            if (Build.VERSION.SDK_INT >= 30 && audioAllowed) {
                type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            }
            startForeground(PROGRESS_NOTIFICATION, progressNotification("准备跟看"), type)
        } else {
            startForeground(PROGRESS_NOTIFICATION, progressNotification("准备跟看"))
        }
    }

    private fun createVirtualDisplay(mediaProjection: MediaProjection) {
        val metrics = resources.displayMetrics
        val scale = minOf(1f, 1280f / maxOf(metrics.widthPixels, metrics.heightPixels).toFloat())
        val width = (metrics.widthPixels * scale).toInt().coerceAtLeast(2)
        val height = (metrics.heightPixels * scale).toInt().coerceAtLeast(2)
        val reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2)
        imageReader = reader
        virtualDisplay = mediaProjection.createVirtualDisplay(
            "vca-follow-watch",
            width,
            height,
            metrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            reader.surface,
            null,
            workerHandler,
        )
    }

    private fun sampleFrame() {
        val context = AssistService.currentContext()
        if (context != null && System.currentTimeMillis() - context.capturedAt < 15_000) {
            if (trackedPackage.isEmpty()) {
                trackedPackage = context.packageName
                watchTitle = context.text
            } else if (trackedPackage != context.packageName &&
                AssistService.isWatchedPackage(context.packageName)
            ) {
                degradations.add("检测到切换视频 App，已停止本次跟看")
                finishCapture()
                return
            }
        }

        val image = runCatching { imageReader?.acquireLatestImage() }.getOrNull() ?: return
        var bitmap: Bitmap? = null
        try {
            image.use {
                val plane = it.planes.firstOrNull() ?: return
                val rowPadding = plane.rowStride - plane.pixelStride * it.width
                val full = Bitmap.createBitmap(
                    it.width + rowPadding / plane.pixelStride,
                    it.height,
                    Bitmap.Config.ARGB_8888,
                )
                full.copyPixelsFromBuffer(plane.buffer)
                bitmap = if (full.width == it.width) {
                    full
                } else {
                    val cropped = Bitmap.createBitmap(full, 0, 0, it.width, it.height)
                    if (cropped !== full) full.recycle()
                    cropped
                }
            }
            val frame = bitmap ?: return
            sampleCount++
            if (isBlack(frame)) {
                blackFrames++
                return
            }

            // JPEG 仅用于内存中去重，压缩结果与 Bitmap 都在本帧处理后释放。
            val jpeg = ByteArrayOutputStream().use { output ->
                frame.compress(Bitmap.CompressFormat.JPEG, 60, output)
                output.toByteArray()
            }
            val digest = MessageDigest.getInstance("SHA-256").digest(jpeg)
                .joinToString("") { "%02x".format(it) }
            if (!frameDigests.add(digest)) return

            val input = InputImage.fromBitmap(frame, 0)
            if (this::textRecognizer.isInitialized) {
                runCatching {
                    val result = Tasks.await(textRecognizer.process(input), 6, TimeUnit.SECONDS)
                    result.text.trim().take(2000).takeIf { it.isNotBlank() }?.let(ocrSamples::add)
                }.onFailure { degradations.add("部分画面 OCR 失败，已保留其他证据") }
            }
            if (this::imageLabeler.isInitialized) {
                runCatching {
                    val labels = Tasks.await(imageLabeler.process(input), 6, TimeUnit.SECONDS)
                    labels.map { it.text.trim() }.filter { it.isNotBlank() }.take(5)
                        .forEach(visualSamples::add)
                }.onFailure { degradations.add("图像标签不可用，已跳过") }
            }
        } catch (_: Exception) {
            degradations.add("部分画面采样失败，已保留其他证据")
        } finally {
            runCatching { bitmap?.recycle() }
        }
    }

    private fun isBlack(bitmap: Bitmap): Boolean {
        val stepX = (bitmap.width / 8).coerceAtLeast(1)
        val stepY = (bitmap.height / 8).coerceAtLeast(1)
        var y = 0
        while (y < bitmap.height) {
            var x = 0
            while (x < bitmap.width) {
                val color = bitmap.getPixel(x, y)
                if (maxOf(Color.red(color), Color.green(color), Color.blue(color)) > 16) return false
                x += stepX
            }
            y += stepY
        }
        return true
    }

    private fun startAudio(mediaProjection: MediaProjection) {
        if (Build.VERSION.SDK_INT < 29) {
            degradations.add("当前系统不支持应用内录，仅使用画面和文字")
            return
        }
        if (!audioAllowed) {
            degradations.add("未授予内录权限，仅使用画面和文字")
            return
        }
        try {
            val config = AudioPlaybackCaptureConfiguration.Builder(mediaProjection)
                .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
                .addMatchingUsage(AudioAttributes.USAGE_GAME)
                .build()
            val format = AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(16_000)
                .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                .build()
            val minBuffer = AudioRecord.getMinBufferSize(
                16_000,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
            )
            val record = AudioRecord.Builder()
                .setAudioPlaybackCaptureConfig(config)
                .setAudioFormat(format)
                .setBufferSizeInBytes(maxOf(minBuffer * 2, 4096))
                .build()
            if (record.state != AudioRecord.STATE_INITIALIZED) {
                throw IllegalStateException("内录音轨初始化失败")
            }
            audioRecord = record
            record.startRecording()
            val pcm = File(cacheDir, "watch_$startedAt.pcm")
            pcmFile = pcm
            audioThread = Thread({
                try {
                    FileOutputStream(pcm).buffered().use { output ->
                        val buffer = ByteArray(4096)
                        while (capturing.get()) {
                            val count = record.read(buffer, 0, buffer.size)
                            if (count > 0) {
                                output.write(buffer, 0, count)
                                if (!audioNonZero) {
                                    for (i in 0 until count) {
                                        if (buffer[i].toInt() != 0) {
                                            audioNonZero = true
                                            break
                                        }
                                    }
                                }
                            } else if (count < 0) {
                                break
                            }
                        }
                    }
                } catch (_: Exception) {
                    if (capturing.get()) degradations.add("内录中断，仅使用画面和文字")
                }
            }, "vca-watch-audio").also { it.start() }
        } catch (_: Exception) {
            degradations.add("源 App 或系统不允许内录，仅使用画面和文字")
            runCatching { audioRecord?.release() }
            audioRecord = null
        }
    }

    private fun finishCapture() {
        if (!capturing.compareAndSet(true, false)) return
        mainHandler.removeCallbacks(countdownRunnable)
        workerHandler?.removeCallbacks(sampleRunnable)
        releaseProjection()
        stopAudio()
        worker?.quitSafely()
        updateProgress("采集完成 · 正在准备分析")

        uploadThread = Thread({
            runCatching { audioThread?.join(2000) }
            runCatching { worker?.join(20_000) }
            if (!alive) return@Thread
            val audio = buildWavIfUsable()
            if (sampleCount == 0) {
                degradations.add("未采集到画面，可能为受保护内容或录屏已停止")
            } else if (blackFrames == sampleCount) {
                degradations.add("画面为黑帧或受 DRM 保护，仅使用文字与语音")
            }
            uploadAndPoll(audio)
        }, "vca-watch-upload").also { it.start() }
    }

    @Synchronized
    private fun releaseProjection() {
        runCatching { virtualDisplay?.release() }
        virtualDisplay = null
        runCatching { imageReader?.close() }
        imageReader = null
        projection?.let {
            runCatching { it.unregisterCallback(projectionCallback) }
            runCatching { it.stop() }
        }
        projection = null
    }

    @Synchronized
    private fun stopAudio() {
        audioRecord?.let {
            runCatching { it.stop() }
            runCatching { it.release() }
        }
        audioRecord = null
    }

    private fun buildWavIfUsable(): File? {
        val pcm = pcmFile
        if (pcm == null || !pcm.exists() || pcm.length() == 0L || !audioNonZero) {
            if (audioAllowed && Build.VERSION.SDK_INT >= 29) {
                degradations.add("未采集到有效音轨，源 App 可能禁止内录或当前静音")
            }
            pcm?.delete()
            return null
        }
        return runCatching {
            val wav = File(cacheDir, "watch_$startedAt.wav")
            DataOutputStream(FileOutputStream(wav).buffered()).use { output ->
                val dataSize = pcm.length()
                output.writeBytes("RIFF")
                output.writeLittleInt(36 + dataSize)
                output.writeBytes("WAVEfmt ")
                output.writeLittleInt(16)
                output.writeLittleShort(1)
                output.writeLittleShort(1)
                output.writeLittleInt(16_000)
                output.writeLittleInt(32_000)
                output.writeLittleShort(2)
                output.writeLittleShort(16)
                output.writeBytes("data")
                output.writeLittleInt(dataSize)
                pcm.inputStream().use { it.copyTo(output) }
            }
            wavFile = wav
            wav
        }.onFailure {
            degradations.add("内录音频整理失败，仅使用画面和文字")
        }.getOrNull()
    }

    private fun DataOutputStream.writeLittleInt(value: Long) {
        write((value and 0xff).toInt())
        write(((value shr 8) and 0xff).toInt())
        write(((value shr 16) and 0xff).toInt())
        write(((value shr 24) and 0xff).toInt())
    }

    private fun DataOutputStream.writeLittleShort(value: Int) {
        write(value and 0xff)
        write((value shr 8) and 0xff)
    }

    private fun uploadAndPoll(audio: File?) {
        val server = Prefs.server(this).trimEnd('/')
        if (server.isEmpty()) {
            audio?.delete()
            pcmFile?.delete()
            failStart("未配置服务地址，无法分析")
            return
        }
        val elapsedSeconds = ((SystemClock.elapsedRealtime() - startedAt) / 1000.0).toInt()
            .coerceIn(1, DURATION_SECONDS)
        val metadata = JSONObject()
            .put("title", watchTitle.take(1500))
            .put("package", trackedPackage)
            .put("duration", elapsedSeconds)
            .put("frames", sampleCount)
            .put("ocr_text", JSONArray(ocrSamples.toList()))
            .put("visual_tags", JSONArray(visualSamples.toList()))
            .put("degradations", JSONArray(degradations.toList()))
            .put("audio_mode", if (audio == null) "unavailable" else "internal")

        if (!alive) {
            audio?.delete()
            pcmFile?.delete()
            return
        }
        val response = try {
            Net.postMultipart(
                Prefs.withToken(this, "$server/api/bridge/watch"),
                mapOf("metadata" to metadata.toString(), "token" to Prefs.token(this)),
                "audio",
                audio,
                connectionCallback = { uploadConnection = it },
            )
        } finally {
            audio?.delete()
            pcmFile?.delete()
            wavFile = null
            pcmFile = null
        }
        val error = response.optString("__error", "")
        if (error.isNotEmpty()) {
            failStart("上传失败：$error")
            return
        }
        val taskId = response.optString("id", "")
        if (taskId.isEmpty()) {
            failStart("服务端未返回跟看任务")
            return
        }

        repeat(240) {
            if (!alive) return
            updateProgress("服务端分析中 · 正在识别语音与分类")
            val task = Net.getJson(Prefs.withToken(this, "$server/api/tasks/$taskId"))
            when (task.optString("status", "")) {
                "done" -> {
                    val category = task.optString("category", "其他")
                    val emoji = task.optString("emoji", "")
                    val confidence = (task.optDouble("confidence", 0.0) * 100).toInt()
                    val notes = task.optJSONArray("degradations")
                    val suffix = if (notes != null && notes.length() > 0) {
                        " · ${notes.optString(0)}"
                    } else {
                        ""
                    }
                    showResult("$emoji $category", "置信度 $confidence%$suffix")
                    return
                }
                "failed" -> {
                    failStart(task.optString("error", "服务端分析失败"))
                    return
                }
            }
            runCatching { Thread.sleep(2500) }
        }
        failStart("分析仍在后台进行，可稍后在控制台查看")
    }

    private fun failStart(message: String) {
        showResult("跟看未完成", message)
    }

    private fun createChannels() {
        notificationManager.createNotificationChannel(
            NotificationChannel(PROGRESS_CHANNEL, "60 秒跟看", NotificationManager.IMPORTANCE_LOW)
        )
        notificationManager.createNotificationChannel(
            NotificationChannel(RESULT_CHANNEL, "跟看分类结果", NotificationManager.IMPORTANCE_DEFAULT)
        )
    }

    private fun progressNotification(text: String): Notification {
        val stopIntent = PendingIntent.getService(
            this,
            31,
            Intent(this, FollowWatchService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, PROGRESS_CHANNEL)
            .setContentTitle("视频分类助手")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .setOngoing(true)
            .addAction(0, "停止", stopIntent)
            .build()
    }

    private fun updateProgress(text: String) {
        if (!alive) return
        runCatching {
            notificationManager.notify(PROGRESS_NOTIFICATION, progressNotification(text))
        }
    }

    private fun showResult(title: String, text: String) {
        if (!alive) return
        alive = false
        val notification = Notification.Builder(this, RESULT_CHANNEL)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setSmallIcon(android.R.drawable.ic_menu_info_details)
            .setAutoCancel(true)
            .build()
        runCatching { stopForeground(true) }
        runCatching { notificationManager.notify(RESULT_NOTIFICATION, notification) }
        stopSelf()
    }

    override fun onDestroy() {
        alive = false
        sessionActive = false
        capturing.set(false)
        runCatching { uploadConnection?.disconnect() }
        uploadThread?.interrupt()
        mainHandler.removeCallbacksAndMessages(null)
        workerHandler?.removeCallbacksAndMessages(null)
        releaseProjection()
        stopAudio()
        worker?.quitSafely()
        runCatching { worker?.join(1500) }
        runCatching { audioThread?.join(500) }
        if (this::textRecognizer.isInitialized) runCatching { textRecognizer.close() }
        if (this::imageLabeler.isInitialized) runCatching { imageLabeler.close() }
        runCatching { pcmFile?.delete() }
        runCatching { wavFile?.delete() }
        pcmFile = null
        wavFile = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
