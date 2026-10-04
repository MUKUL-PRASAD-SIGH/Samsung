package com.samsung.interruptible

import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import com.samsung.interruptible.ui.AgentApp
import com.samsung.interruptible.ui.AgentTheme

class MainActivity : ComponentActivity() {
    private val viewModel: AgentViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Hands-free listening is pointless if the screen sleeps mid-conversation.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContent {
            AgentTheme {
                AgentApp(viewModel.controller)
            }
        }
    }
}
