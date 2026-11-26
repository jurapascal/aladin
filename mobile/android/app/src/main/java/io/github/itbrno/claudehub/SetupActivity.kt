package io.github.itbrno.claudehub

import android.content.Intent
import android.os.Bundle
import android.webkit.CookieManager
import android.widget.Button
import android.widget.EditText
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions

/**
 * První obrazovka: spárování s hubem.
 *
 * Ukáže se jen dokud aplikace adresu nemá — potom se rovnou přeskočí na
 * [HubActivity] a člověk ji vidí až kdyby se od hubu odpojil.
 */
class SetupActivity : AppCompatActivity() {

    companion object {
        const val FORGET = "io.github.itbrno.claudehub.FORGET"
    }

    private val scanner = registerForActivityResult(ScanContract()) { result ->
        val text = result.contents ?: return@registerForActivityResult
        connect(text)
    }

    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        // Zkratka z podržené ikony na ploše: zapomenout hub a spárovat znovu.
        if (intent?.action == FORGET) {
            Hub.forget(this)
            CookieManager.getInstance().removeAllCookies(null)
        } else {
            Hub.saved(this)?.let { open(it); return }
        }

        setContentView(R.layout.activity_setup)
        val field = findViewById<EditText>(R.id.url)

        findViewById<Button>(R.id.scan).setOnClickListener {
            scanner.launch(ScanOptions().apply {
                setPrompt(getString(R.string.scan_prompt))
                setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                setBeepEnabled(false)
                setOrientationLocked(false)
            })
        }
        findViewById<Button>(R.id.connect).setOnClickListener {
            connect(field.text.toString())
        }
    }

    private fun connect(raw: String) {
        val url = Hub.normalise(raw)
        if (url == null) {
            Toast.makeText(this, R.string.setup_bad_url, Toast.LENGTH_LONG).show()
            return
        }
        Hub.save(this, url)
        open(url)
    }

    private fun open(url: String) {
        startActivity(Intent(this, HubActivity::class.java)
            .putExtra(HubActivity.EXTRA_URL, url))
        finish()
    }
}
