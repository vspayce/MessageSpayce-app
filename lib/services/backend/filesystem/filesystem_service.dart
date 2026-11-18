import 'package:bluebubbles/helpers/helpers.dart';
import 'package:bluebubbles/database/database.dart' as db;
import 'package:bluebubbles/services/ui/contact_service.dart';
import 'package:bluebubbles/utils/logger/logger.dart';
import 'package:collection/collection.dart';
import 'package:device_info_plus/device_info_plus.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:get/get.dart';
import 'package:idb_shim/idb.dart' as idb;
import 'package:idb_shim/idb_browser.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:path/path.dart';
import 'package:path_provider/path_provider.dart';
import 'package:slugify/slugify.dart';
import 'package:universal_io/io.dart';

FilesystemService fs = Get.isRegistered<FilesystemService>() ? Get.find<FilesystemService>() : Get.put(FilesystemService());

class FilesystemService extends GetxService {
  late Directory appDocDir;
  late final PackageInfo packageInfo;
  AndroidDeviceInfo? androidInfo;
  late final idb.Database webDb;
  late final Uint8List noVideoPreviewIcon;
  late final Uint8List unplayableVideoIcon;
  final RxBool fontExistsOnDisk = false.obs;

  Future<String> get downloadsDirectory async {
    if (kIsWeb) throw "Cannot get downloads directory on web!";

    String filePath = "/storage/emulated/0/Download/";
    if (kIsDesktop) {
      filePath = (await getDownloadsDirectory())!.path;
    }

    return filePath;
  }

  Future<void> init({bool headless = false}) async {
    if (!kIsWeb) {
      //ignore: unnecessary_cast, we need this as a workaround
      appDocDir = (kIsDesktop ? await getApplicationSupportDirectory() : await getApplicationDocumentsDirectory()) as Directory;
      // Sculpin: never adopt another app's folder directly (upstream used the BlueBubbles
      // Microsoft Store folder in place). Instead, copy an existing OpenBubbles install once.
      if (kIsDesktop) {
        await _importFromOpenBubbles();
      }
      if (!headless) {
        final file = await rootBundle.load("assets/images/no-video-preview.png");
        noVideoPreviewIcon = file.buffer.asUint8List();
        final file2 = await rootBundle.load("assets/images/unplayable-video.png");
        unplayableVideoIcon = file2.buffer.asUint8List();
      }
    }
    packageInfo = await PackageInfo.fromPlatform();
    if (!headless && Platform.isAndroid) {
      androidInfo = await DeviceInfoPlugin().androidInfo;
    }
  }

  /// True when this launch copied data over from an OpenBubbles install.
  bool importedFromOpenBubbles = false;

  /// Sculpin: on first launch, copy an existing OpenBubbles install (messages, settings and
  /// iMessage identity) into Sculpin's own data folder. The OpenBubbles folder is left untouched.
  /// Quit OpenBubbles before the first launch so its database isn't mid-write during the copy.
  Future<void> _importFromOpenBubbles() async {
    try {
      final Directory dest = appDocDir;
      if (dest.existsSync() && dest.listSync().isNotEmpty) return; // already set up

      final List<Directory> candidates = [];
      if (Platform.isWindows) {
        // %APPDATA%\Sculpin\sculpin  ->  %APPDATA%\OpenBubbles\openbubbles
        candidates.add(Directory(join(dest.parent.parent.path, "OpenBubbles", "openbubbles")));
      } else if (Platform.isLinux) {
        // ~/.local/share/io.github.vspayce.Sculpin  ->  OpenBubbles' application-id or binary-name folder
        candidates.add(Directory(join(dest.parent.path, "app.bluebubbles.BlueBubbles")));
        candidates.add(Directory(join(dest.parent.path, "bluebubbles")));
      }

      Directory? source;
      for (final d in candidates) {
        if (Directory(join(d.path, "objectbox")).existsSync()) {
          source = d;
          break;
        }
      }
      if (source == null) return;

      Logger.info("Importing OpenBubbles data from ${source.path}");
      // Copy into a temporary folder first, so an interrupted copy is never mistaken for a finished one.
      final Directory tmp = Directory("${dest.path}.importing");
      if (tmp.existsSync()) tmp.deleteSync(recursive: true);
      await _copyDirectory(source, tmp);
      if (dest.existsSync()) dest.deleteSync(recursive: true); // empty at this point
      await tmp.rename(dest.path);
      importedFromOpenBubbles = true;
      Logger.info("Imported OpenBubbles data into ${dest.path}");
    } catch (e, s) {
      Logger.error("Importing from OpenBubbles failed", error: e, trace: s);
    }
  }

  Future<void> _copyDirectory(Directory from, Directory to) async {
    await to.create(recursive: true);
    await for (final entity in from.list(followLinks: false)) {
      final String target = join(to.path, basename(entity.path));
      if (entity is Directory) {
        await _copyDirectory(entity, Directory(target));
      } else if (entity is File) {
        await entity.copy(target);
      }
    }
  }

  void checkFont() async {
    if (!kIsWeb) {
      final file = File("${fs.appDocDir.path}/font/apple.ttf");
      final exists = await file.exists();
      if (exists) {
        final bytes = await file.readAsBytes();
        fontExistsOnDisk.value = true;
        final fontLoader = FontLoader("Apple Color Emoji");
        final cachedFontBytes = ByteData.view(bytes.buffer);
        fontLoader.addFont(
          Future<ByteData>.value(cachedFontBytes),
        );
        await fontLoader.load();
      }
    } else {
      final idbFactory = idbFactoryBrowser;
      idbFactory.open("BlueBubbles.db", version: 1, onUpgradeNeeded: (idb.VersionChangeEvent e) {
        final db = (e.target as idb.OpenDBRequest).result;
        if (!db.objectStoreNames.contains("BBStore")) {
          db.createObjectStore("BBStore");
        }
      }).then((_db) async {
        webDb = _db;
        final txn = webDb.transaction("BBStore", idb.idbModeReadOnly);
        final store = txn.objectStore("BBStore");
        Uint8List? bytes = await store.getObject("iosFont") as Uint8List?;
        await txn.completed;

        if (!isNullOrEmpty(bytes)) {
          fontExistsOnDisk.value = true;
          final fontLoader = FontLoader("Apple Color Emoji");
          final cachedFontBytes = ByteData.view(bytes!.buffer);
          fontLoader.addFont(
            Future<ByteData>.value(cachedFontBytes),
          );
          await fontLoader.load();
        }
      });
    }
  }

  void deleteDB() {
    if (kIsWeb) return;
    db.Database.reset();
    cs.contacts.clear();
  }

  String uriToFilename(String? uri, String? mimeType) {
    // Handle any unknown cases
    String? ext = mimeType != null ? mimeType.split('/')[1] : null;
    ext = (ext != null && ext.contains('+')) ? ext.split('+')[0] : ext;
    if (uri == null) return (ext != null) ? 'unknown.$ext' : 'unknown';

    // Get the filename
    String filename = uri;
    if (filename.contains('/')) {
      filename = filename.split('/').last;
    }

    // Get the extension
    if (filename.contains('.')) {
      List<String> split = filename.split('.');
      ext = split[1];
      filename = split[0];
    }

    // Slugify the filename
    filename = slugify(filename, delimiter: '_');

    // Rebuild the filename
    return (ext != null && ext.isNotEmpty) ? '$filename.$ext' : filename;
  }

  void copyDirectory(Directory source, Directory destination) =>
    source.listSync(recursive: false).forEach((element) async {
      if (element is Directory) {
        Directory newDirectory = Directory(join(destination.absolute.path, basename(element.path)));
        newDirectory.createSync();
        Logger.info("Created new directory ${basename(element.path)}");

        copyDirectory(element.absolute, newDirectory);
      } else if (element is File) {
        element.copySync(join(destination.path, basename(element.path)));
        Logger.info("Created file ${basename(element.path)}");
      }
    });

  Future<String> saveToDownloads(File file) async {
    if (kIsWeb) throw "Cannot save file on web!";

    final String filename = basename(file.path);
    final String downloadsDir = await downloadsDirectory;
    final String newPath = join(downloadsDir, filename);
    await file.copy(newPath);
    return newPath;
  }
}