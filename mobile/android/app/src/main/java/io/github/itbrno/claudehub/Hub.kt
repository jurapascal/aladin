package io.github.itbrno.claudehub

import android.content.Context
import android.net.Uri

/**
 * Kam se má aplikace připojit.
 *
 * Adresu hubu i s tokenem drží obyčejné SharedPreferences — je to jediné, co si
 * telefon pamatuje. Token v ní je citlivý stejně jako heslo, takže se nikam
 * neloguje a v aplikaci se ukazuje jen v poli, kam ho člověk sám vložil.
 */
object Hub {

    private const val PREFS = "hub"
    private const val KEY_URL = "url"

    fun saved(context: Context): String? =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_URL, null)

    fun save(context: Context, url: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putString(KEY_URL, url).apply()
    }

    fun forget(context: Context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().remove(KEY_URL).apply()
    }

    /** Přijme jen to, co opravdu vypadá jako párovací adresa hubu. */
    fun normalise(raw: String): String? {
        val text = raw.trim()
        if (text.isEmpty()) return null
        val uri = runCatching { Uri.parse(text) }.getOrNull() ?: return null
        val scheme = uri.scheme?.lowercase()
        if (scheme != "http" && scheme != "https") return null
        if (uri.host.isNullOrBlank()) return null
        return text
    }

    /** Stránky mimo hub patří do prohlížeče, ne do okna aplikace. */
    fun sameHost(base: String, candidate: String): Boolean {
        val a = runCatching { Uri.parse(base).host }.getOrNull() ?: return false
        val b = runCatching { Uri.parse(candidate).host }.getOrNull() ?: return false
        return a.equals(b, ignoreCase = true)
    }
}
