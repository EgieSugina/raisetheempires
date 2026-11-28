zynga = {
	"ads": {
		"WatchToEarn": {
			"service": {
				"available": function () {
					console.log("W2E avail");
					return false;
				},
				"initializeFlash": function (oid, a, b, c) {
					console.log("initF " + oid + ", " + a + ", " + b + ", " + c);
					return false;
				}
			}
		}
	}
}

statTracker = {
	"logWorldObjectCount": function () {
		console.log("World Count");
		return;
	}
}

ZYFrameManager = {
	"reloadApp": function () {
		console.log("Reload App");
		window.location.reload();
		return;
	},
	"navigateTo": function (a, b, c) {
		console.log("Navigate To" + a + " - " + b + " - " + c);
		return;
	},
	"openTab": function (a, b, c) {
		console.log("Open Tab" + a + " - " + b + " - " + c);
		return;
	},
	"switchToTab": function (a) {
		console.log("Switch To Tab" + a);
		return;
	}

}

function setLoadingStatus(msg, pct, detail) {
	if (window.__loadingDone) return;
	if (msg) document.getElementById("loading_message").textContent = msg;
	if (typeof pct === "number") {
		var bar = document.getElementById("inner_progress_bar");
		var cur = parseFloat(bar.style.width) || 0;
		if (pct > cur) bar.style.width = Math.min(100, pct) + "%";
	}
	if (detail) {
		var el = document.getElementById("loading_detail");
		if (el) el.textContent = detail;
	}
}

function inner_getUserInfo() {
	setLoadingStatus("Loading User info...", 35, "Memuat profil & data pemain...");
	document.getElementById("flash_enabler").style.display = "none";
}

function inner_getFriendData() {
	setLoadingStatus("Loading Friends data...", 50, "Memuat daftar teman & tetangga...");
}

function inner_getAppFriendIds() {
	setLoadingStatus("Loading Game World...", 65, "Memuat pulau, bangunan & unit...");
}

function inner_onGameLoaded(seen, popp, canvas) {
	window.__loadingDone = true;
	document.getElementById("inner_progress_bar").style.width = "100%";
	document.getElementById("loading_game").style.display = "none";
}

function openInAppPurchaseAPI(gid, snid, snuid, cid, a, b, c) {
	console.log("Purchase " + gid + ", " + snid + ", " + snuid + ", " + cid + ", " + a + ", " + b + ", " + c)
	return;
}

function hasPermission(perm, snuid, name) {
	console.log("Perm " + perm + ", " + snuid + ", " + name)
	return;
}

function showPermissions(d) {
	console.log("Show Perm " + d)
	return;
}