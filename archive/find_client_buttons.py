from pywinauto import Application

# 1. Connect to or start the application
# Use start() to launch a new app, or connect() to attach to a running one
app = Application(backend="uia").connect(title_re="Botting Hub Client v3.0.43")

# 2. Access the main window (dialog)
dlg = app.window(title_re="Botting Hub Client v3.0.43")

# 3. Print control identifiers to see all elements and their properties
dlg.print_control_identifiers()
