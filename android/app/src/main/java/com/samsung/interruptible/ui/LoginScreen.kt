package com.samsung.interruptible.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.samsung.interruptible.data.Urls
import kotlinx.coroutines.launch

val Gold = Color(0xFFE8B84A)
private val Serif = FontFamily.Serif

/** The wordmark: KAIROS with its Greek form beneath. Shared by the login screen, the top bar and the empty chat. */
@Composable
fun Wordmark(big: Boolean = true) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text("KAIROS", color = Gold, fontFamily = Serif, fontWeight = FontWeight.SemiBold, fontSize = if (big) 40.sp else 15.sp, letterSpacing = if (big) 8.sp else 3.sp)
        Text("ΚΑΙΡΟΣ", color = Gold.copy(alpha = 0.6f), fontFamily = Serif, fontSize = if (big) 14.sp else 9.sp, letterSpacing = if (big) 6.sp else 2.sp)
    }
}

/**
 * First thing a new install shows when the server needs a key: server address + access key, verified before anything is
 * stored. (A phone has to be told where the server is, so the address is on this screen, not buried in Settings.)
 */
@Composable
fun LoginScreen(initialServer: String, notice: String, firstRun: Boolean = false, onSignIn: suspend (server: String, key: String) -> String?) {
    // On a first run the emulator's address is a useless default for a phone: start empty so the hint below is what the user sees.
    var server by remember { mutableStateOf(if (firstRun) "" else initialServer) }
    var key by remember { mutableStateOf("") }
    var show by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf(notice) }
    val scope = rememberCoroutineScope()

    fun submit() {
        if (busy || server.isBlank() || (!firstRun && key.isBlank())) return
        busy = true
        error = ""
        scope.launch {
            error = onSignIn(server, key).orEmpty()
            busy = false
        }
    }

    // A Surface supplies the light content colour; without it every plain Text here would be black on black.
    androidx.compose.material3.Surface(color = Palette.Background, contentColor = Color(0xFFE2E8F0), modifier = Modifier.fillMaxSize()) {
    Box(
        Modifier.fillMaxSize().background(Brush.verticalGradient(listOf(Color(0xFF14110A), Palette.Background))).padding(24.dp),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            Modifier.fillMaxWidth().background(Palette.Surface, RoundedCornerShape(28.dp)).padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            Wordmark()
            Text(if (firstRun) "Connect to Kairos" else "Welcome back", fontSize = 20.sp, fontWeight = FontWeight.SemiBold)
            Text(
                if (firstRun) "Kairos runs on a computer; this app is its remote. Enter that computer's address (shown when you start Kairos there)."
                else "Enter your server and access key.",
                color = Palette.Muted, fontSize = 13.sp,
            )
            OutlinedTextField(
                server, { server = it }, label = { Text("Server") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Next),
                supportingText = { Text(if (firstRun) "For example ws://192.168.1.20:8000 (your computer's address on the same Wi-Fi)" else "From the emulator, ws://10.0.2.2:8000 is your computer") },
            )
            OutlinedTextField(
                key, { key = it; error = "" }, label = { Text(if (firstRun) "Access key (if the computer asks for one)" else "Access key") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                visualTransformation = if (show) VisualTransformation.None else PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Go),
                keyboardActions = KeyboardActions(onGo = { submit() }),
                isError = error.isNotEmpty(),
                trailingIcon = { TextButton(onClick = { show = !show }) { Text(if (show) "Hide" else "Show", fontSize = 12.sp) } },
            )
            if (Urls.isInsecureRemote(server)) {
                Text("This address is not encrypted: your key would cross the network in the clear. Prefer wss://.", color = Palette.Amber, fontSize = 12.sp)
            }
            if (error.isNotEmpty()) Text(error, color = Palette.Rose, fontSize = 13.sp, modifier = Modifier.fillMaxWidth())
            Button(
                onClick = ::submit, enabled = server.isNotBlank() && (firstRun || key.isNotBlank()) && !busy, modifier = Modifier.fillMaxWidth().size(height = 48.dp, width = 0.dp),
                colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Color(0xFF14110A)),
            ) {
                if (busy) CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp, color = Color(0xFF14110A)) else Text(if (firstRun) "Connect" else "Continue", fontWeight = FontWeight.SemiBold)
            }
        }
    }
    }
}
