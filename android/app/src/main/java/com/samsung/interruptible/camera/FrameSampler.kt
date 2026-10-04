package com.samsung.interruptible.camera

import android.graphics.BitmapFactory
import android.graphics.ImageFormat
import android.graphics.Matrix
import android.graphics.Rect
import android.graphics.YuvImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import java.io.ByteArrayOutputStream

/**
 * Turns the camera preview into the small JPEG frames the server's vision tool wants: at most one frame per second,
 * longest side <= 1024 px, JPEG quality ~70. The server only runs the vision model when a question needs the image, so
 * sending frames costs almost nothing until then.
 */
class FrameSampler(
    private val intervalMs: Long = 1_000,
    private val maxSide: Int = 1_024,
    private val quality: Int = 70,
    private val clock: () -> Long = System::currentTimeMillis,
    private val onFrame: (jpeg: ByteArray, width: Int, height: Int) -> Unit,
) : ImageAnalysis.Analyzer {
    private var lastAt = 0L

    override fun analyze(image: ImageProxy) {
        try {
            val now = clock()
            if (now - lastAt < intervalMs) return
            lastAt = now
            encode(image)?.let { (jpeg, w, h) -> onFrame(jpeg, w, h) }
        } finally {
            image.close()
        }
    }

    private fun encode(image: ImageProxy): Triple<ByteArray, Int, Int>? {
        val nv21 = yuv420ToNv21(image) ?: return null
        val full = ByteArrayOutputStream().also {
            YuvImage(nv21, ImageFormat.NV21, image.width, image.height, null).compressToJpeg(Rect(0, 0, image.width, image.height), 90, it)
        }.toByteArray()
        val bitmap = BitmapFactory.decodeByteArray(full, 0, full.size) ?: return null
        val matrix = Matrix().apply {
            postRotate(image.imageInfo.rotationDegrees.toFloat())            // upright, whatever the sensor orientation
            val scale = maxSide.toFloat() / maxOf(bitmap.width, bitmap.height)
            if (scale < 1f) postScale(scale, scale)
        }
        val out = android.graphics.Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true)
        val bytes = ByteArrayOutputStream().also { out.compress(android.graphics.Bitmap.CompressFormat.JPEG, quality, it) }.toByteArray()
        val result = Triple(bytes, out.width, out.height)
        if (out !== bitmap) bitmap.recycle()
        out.recycle()
        return result
    }

    companion object {
        /** YUV_420_888 (planar, arbitrary row/pixel strides) -> NV21 (Y plane followed by interleaved V,U). */
        fun yuv420ToNv21(image: ImageProxy): ByteArray? {
            if (image.format != ImageFormat.YUV_420_888 || image.planes.size < 3) return null
            val w = image.width
            val h = image.height
            val out = ByteArray(w * h * 3 / 2)
            val y = image.planes[0]
            var pos = 0
            for (row in 0 until h) {
                for (col in 0 until w) out[pos++] = y.buffer.get(row * y.rowStride + col * y.pixelStride)
            }
            val u = image.planes[1]
            val v = image.planes[2]
            for (row in 0 until h / 2) {
                for (col in 0 until w / 2) {
                    out[pos++] = v.buffer.get(row * v.rowStride + col * v.pixelStride)
                    out[pos++] = u.buffer.get(row * u.rowStride + col * u.pixelStride)
                }
            }
            return out
        }
    }
}
