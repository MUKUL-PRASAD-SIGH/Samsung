package com.samsung.interruptible.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.samsung.interruptible.data.KeysStatus
import com.samsung.interruptible.data.Urls
import kotlinx.coroutines.launch

/**
 * Where the user gives Kairos their own Groq / OpenRouter API keys. The agent runs on the server this phone is paired with, so
 * the keys are sent there (it checks them with the provider before keeping them) and are never shown again, only a masked hint.
 *
 * `onSave(groq, openrouter)` returns an error message, or null on success. A blank field means "leave that provider alone".
 */
@Composable
fun KeysForm(
    status: KeysStatus?,
    serverUrl: String,
    onSave: suspend (groq: String?, openrouter: String?) -> String?,
    onClose: () -> Unit,
) {
    var groq by remember { mutableStateOf("") }
    var openrouter by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf("") }
    var saved by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val clearText = Urls.isInsecureRemote(serverUrl)

    fun submit() {
        if (busy || (groq.isBlank() && openrouter.isBlank())) return
        busy = true
        error = ""
        saved = false
        scope.launch {
            val result = onSave(groq.takeIf { it.isNotBlank() }, openrouter.takeIf { it.isNotBlank() })
            busy = false
            if (result == null) {
                saved = true
                groq = ""
                openrouter = ""
            } else {
                error = result
            }
        }
    }

    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("API keys", fontSize = 20.sp, fontWeight = FontWeight.SemiBold)
        Text(
            "Add a Groq and/or OpenRouter key. Either one is enough; if both are set, Groq is used for replies. " +
                "The keys are stored on the Kairos server this phone is connected to.",
            fontSize = 12.sp, color = Palette.Muted,
        )
        KeyField("Groq key", "gsk_…", groq, { groq = it; error = "" }, status?.takeIf { it.groqConfigured }?.groqHint)
        KeyField("OpenRouter key", "sk-or-…", openrouter, { openrouter = it; error = "" }, status?.takeIf { it.openrouterConfigured }?.openrouterHint)
        if (clearText) {
            Text("Your server address is not encrypted: on a home network that is usually fine, but do not do this on public Wi-Fi.",
                color = Palette.Amber, fontSize = 12.sp)
        }
        if (error.isNotBlank()) Text(error, color = Palette.Rose, fontSize = 13.sp)
        if (saved) Text("Saved. Kairos is now using your key.", color = Palette.Emerald, fontSize = 13.sp)
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
            TextButton(onClick = onClose) { Text(if (saved) "Done" else "Close") }
            TextButton(onClick = ::submit, enabled = !busy && (groq.isNotBlank() || openrouter.isNotBlank())) {
                if (busy) CircularProgressIndicator(Modifier.padding(end = 8.dp), strokeWidth = 2.dp) 
                Text(if (busy) "Checking…" else "Save keys")
            }
        }
    }
}

@Composable
private fun KeyField(label: String, placeholder: String, value: String, onChange: (String) -> Unit, savedHint: String?) {
    OutlinedTextField(
        value, onChange, label = { Text(label) }, singleLine = true,
        placeholder = { Text(if (savedHint != null) "Enter a new key to replace it" else placeholder) },
        visualTransformation = PasswordVisualTransformation(),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, autoCorrectEnabled = false),
        supportingText = { if (savedHint != null) Text("Saved: $savedHint") },
        modifier = Modifier.fillMaxWidth(),
    )
}

@Composable
fun KeysDialog(
    status: KeysStatus?,
    serverUrl: String,
    onSave: suspend (String?, String?) -> String?,
    onDismiss: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        text = { KeysForm(status, serverUrl, onSave, onDismiss) },
        confirmButton = {},
    )
}
