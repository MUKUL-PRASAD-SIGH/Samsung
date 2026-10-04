package com.samsung.interruptible.ui

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AccountTree
import androidx.compose.material.icons.filled.Chat
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Timeline
import androidx.compose.material.icons.filled.Tune
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.samsung.interruptible.data.ConnState
import com.samsung.interruptible.state.AgentController

private enum class Tab(val label: String) { Chat("Chat"), State("State"), Trace("Trace"), Graph("Graph") }

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AgentApp(controller: AgentController) {
    val state by controller.state.collectAsStateWithLifecycle()
    var tab by rememberSaveable { mutableStateOf(Tab.Chat) }
    var showSettings by rememberSaveable { mutableStateOf(false) }
    val context = LocalContext.current

    // Runtime permissions: ask when the user first reaches for the mic / camera, then do what they asked.
    val micPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) controller.setHandsFree(true)
    }
    val cameraPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) controller.setSharing(true)
    }
    fun has(permission: String) = ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED

    Scaffold(
        containerColor = Palette.Background,
        topBar = {
            TopAppBar(
                colors = TopAppBarDefaults.topAppBarColors(containerColor = Palette.Background),
                title = {
                    Column {
                        Text("Interruptible Agent", fontWeight = FontWeight.SemiBold, fontSize = 17.sp)
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Box(Modifier.size(8.dp).background(connectionColor(state.connection), CircleShape))
                            Text("  ${connectionLabel(state.connection)} · epoch ${state.epoch}", fontSize = 11.sp, color = Palette.Muted)
                        }
                    }
                },
                actions = {
                    IconButton(onClick = { showSettings = true }) { Icon(Icons.Filled.Settings, contentDescription = "Settings") }
                },
            )
        },
        bottomBar = {
            NavigationBar(containerColor = Palette.Surface) {
                Tab.entries.forEach { t ->
                    NavigationBarItem(
                        selected = tab == t,
                        onClick = { tab = t },
                        icon = {
                            Icon(
                                when (t) {
                                    Tab.Chat -> Icons.Filled.Chat
                                    Tab.State -> Icons.Filled.Tune
                                    Tab.Trace -> Icons.Filled.Timeline
                                    Tab.Graph -> Icons.Filled.AccountTree
                                },
                                contentDescription = t.label,
                            )
                        },
                        label = { Text(t.label) },
                    )
                }
            }
        },
    ) { padding ->
        Box(Modifier.padding(padding).fillMaxSize()) {
            when (tab) {
                Tab.Chat -> ChatScreen(
                    state = state,
                    onSend = controller::sendText,
                    onStop = controller::interrupt,
                    onToggleMic = {
                        if (state.handsFree) controller.setHandsFree(false)
                        else if (has(Manifest.permission.RECORD_AUDIO)) controller.setHandsFree(true)
                        else micPermission.launch(Manifest.permission.RECORD_AUDIO)
                    },
                    onToggleSpeak = { controller.setSpeakReplies(!state.speakReplies) },
                    onToggleCamera = {
                        if (state.sharing) controller.setSharing(false)
                        else if (has(Manifest.permission.CAMERA)) controller.setSharing(true)
                        else cameraPermission.launch(Manifest.permission.CAMERA)
                    },
                    onFrame = { jpeg, w, h -> controller.sendFrame(jpeg, w, h) },
                    onDismissError = controller::dismissError,
                )
                Tab.State -> StatePanel(state)
                Tab.Trace -> TracePanel(state)
                Tab.Graph -> GraphPanel(state)
            }
        }
    }

    if (showSettings) {
        SettingsDialog(
            initial = controller.settings,
            onDismiss = { showSettings = false },
            onSave = {
                controller.applySettings(it)
                showSettings = false
            },
        )
    }
}

internal fun connectionColor(c: ConnState) = when (c) {
    ConnState.Connected -> Palette.Emerald
    ConnState.Connecting -> Palette.Amber
    else -> Palette.Rose
}

internal fun connectionLabel(c: ConnState) = when (c) {
    ConnState.Connected -> "connected"
    ConnState.Connecting -> "connecting"
    ConnState.Disconnected -> "disconnected"
    is ConnState.Refused -> "refused — check Settings"
    is ConnState.Retrying -> "reconnecting"
}

@Composable
internal fun SectionTitle(text: String) {
    Text(text.uppercase(), fontSize = 11.sp, color = Palette.Muted, fontWeight = FontWeight.SemiBold,
        modifier = Modifier.padding(top = 12.dp, bottom = 4.dp))
}

