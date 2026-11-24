package com.vca.assistant

import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/** 极简 HTTP 工具，避免引入额外依赖 */
object Net {
    private val URL_REGEX = Regex("""https?://[^\s，。；、）】"']+""")

    fun extractUrl(text: String): String? {
        val raw = URL_REGEX.find(text)?.value ?: return null
        return raw.trimEnd('/', '.', ',', ')', ']', '，', '。')
    }

    /** 失败时返回带 __error 字段的 JSONObject，调用方无需处理异常 */
    fun postJson(url: String, body: JSONObject, timeoutMs: Int = 12000): JSONObject {
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = timeoutMs
                readTimeout = timeoutMs
                doOutput = true
                setRequestProperty("Content-Type", "application/json; charset=utf-8")
                setRequestProperty("Accept", "application/json")
            }
            conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: ""
            if (code in 200..299) {
                if (text.isBlank()) JSONObject() else JSONObject(text)
            } else {
                JSONObject().put("__error", "HTTP $code ${text.take(120)}")
            }
        } catch (e: Exception) {
            JSONObject().put("__error", e.message ?: e.javaClass.simpleName)
        } finally {
            conn?.disconnect()
        }
    }

    fun getJson(url: String, timeoutMs: Int = 12000): JSONObject {
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                connectTimeout = timeoutMs
                readTimeout = timeoutMs
                setRequestProperty("Accept", "application/json")
            }
            readResponse(conn)
        } catch (e: Exception) {
            JSONObject().put("__error", e.message ?: e.javaClass.simpleName)
        } finally {
            conn?.disconnect()
        }
    }

    fun postMultipart(
        url: String,
        fields: Map<String, String>,
        fileField: String,
        file: File?,
        timeoutMs: Int = 60000,
        connectionCallback: ((HttpURLConnection?) -> Unit)? = null,
    ): JSONObject {
        var conn: HttpURLConnection? = null
        return try {
            val boundary = "----VCA${System.nanoTime()}"
            conn = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = timeoutMs
                readTimeout = timeoutMs
                doOutput = true
                setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
                setRequestProperty("Accept", "application/json")
            }
            connectionCallback?.invoke(conn)
            java.io.DataOutputStream(conn.outputStream.buffered()).use { output ->
                fun text(value: String) = output.write(value.toByteArray(Charsets.UTF_8))
                fields.forEach { (name, value) ->
                    text("--$boundary\r\nContent-Disposition: form-data; name=\"$name\"\r\n\r\n$value\r\n")
                }
                if (file != null && file.exists()) {
                    text(
                        "--$boundary\r\nContent-Disposition: form-data; name=\"$fileField\"; " +
                            "filename=\"watch.wav\"\r\nContent-Type: audio/wav\r\n\r\n"
                    )
                    file.inputStream().use { it.copyTo(output) }
                    text("\r\n")
                }
                text("--$boundary--\r\n")
                output.flush()
            }
            readResponse(conn)
        } catch (e: Exception) {
            JSONObject().put("__error", e.message ?: e.javaClass.simpleName)
        } finally {
            connectionCallback?.invoke(null)
            conn?.disconnect()
        }
    }

    private fun readResponse(conn: HttpURLConnection): JSONObject {
        val code = conn.responseCode
        val stream = if (code in 200..299) conn.inputStream else conn.errorStream
        val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: ""
        return if (code in 200..299) {
            if (text.isBlank()) JSONObject() else JSONObject(text)
        } else {
            JSONObject().put("__error", "HTTP $code ${text.take(160)}")
        }
    }
}
